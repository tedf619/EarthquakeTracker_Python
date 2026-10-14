import datetime
import io
import json
import math
import queue
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from PIL import Image, ImageTk


# =====================================================================
# Base MapControl Class
# =====================================================================
class MapControl(tk.Canvas):

    def __init__(self, master, width: int = 800, height: int = 600, **kwargs):
        super().__init__(master, width=width, height=height, **kwargs)

        self.root = master
        self.width = width
        self.height = height
        self.zoom = 5
        self.min_zoom = 2
        self.max_zoom = 18
        self.last_tile_cache_size_reported = 0

        self.center_lat = 36.7783  # California center
        self.center_lon = -119.4179

        # Cursors
        self.default_cursor = ""
        self.busy_cursor = "watch"

        # Tile download cache & thread queue
        self.tile_cache = {}
        self.tile_queue = queue.Queue()

        # Drag state tracking
        self._drag_start_x = 0
        self._drag_start_y = 0

        # Events
        self.on_zoom_changed = None
        self.on_status_message = None

        # Bind native canvas events
        self.bind("<Configure>", self.self__configure)
        self.bind("<ButtonPress-1>", self.self__mouse_click)
        self.bind("<B1-Motion>", self.self__mouse_drag)

        # Mouse wheel binds across platforms
        self.bind("<MouseWheel>", self.self__mouse_wheel)  # Win/macOS
        self.bind("<Button-4>", lambda e: self.self__zoom(1, e.x, e.y))  # Linux wheel up
        self.bind("<Button-5>", lambda e: self.self__zoom(-1, e.x, e.y))  # Linux wheel down

        # Start tile queue polling loop on UI thread
        self._poll_tile_queue()

    # -----------------------------------------------------------------
    # Web Mercator Projection Math
    # -----------------------------------------------------------------
    def latlon_to_pixel(self, lat: float, lon: float, zoom: int = None) -> tuple[float, float]:
        if zoom is None:
            zoom = self.zoom
        lat = max(min(lat, 85.05112878), -85.05112878)
        lat_rad = math.radians(lat)
        n = 2.0**zoom
        x = (lon + 180.0) / 360.0 * n * 256.0
        y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n * 256.0
        return x, y

    def pixel_to_latlon(self, px: float, py: float, zoom: int = None) -> tuple[float, float]:
        if zoom is None:
            zoom = self.zoom
        n = 2.0**zoom
        lon = (px / (n * 256.0)) * 360.0 - 180.0
        lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * py / (n * 256.0))))
        lat = math.degrees(lat_rad)
        return lat, lon

    def get_screen_coords(self, lat: float, lon: float) -> tuple[float, float]:
        gx, gy = self.latlon_to_pixel(lat, lon)
        cx, cy = self.latlon_to_pixel(self.center_lat, self.center_lon)

        screen_x = (gx - cx) + (self.width / 2)
        screen_y = (gy - cy) + (self.height / 2)
        return screen_x, screen_y

    def screen_to_latlon(self, screen_x: float, screen_y: float) -> tuple[float, float]:
        cx, cy = self.latlon_to_pixel(self.center_lat, self.center_lon)
        gx = cx + (screen_x - (self.width / 2))
        gy = cy + (screen_y - (self.height / 2))
        return self.pixel_to_latlon(gx, gy)

    # -----------------------------------------------------------------
    # Pan & Zoom Logic
    # -----------------------------------------------------------------
    def pan_by_pixels(self, dx: float, dy: float) -> None:
        cx, cy = self.latlon_to_pixel(self.center_lat, self.center_lon)
        new_cx = cx - dx
        new_cy = cy - dy
        self.center_lat, self.center_lon = self.pixel_to_latlon(new_cx, new_cy)

    def zoom_at_screen_pos(self, delta_zoom: int, screen_x: float, screen_y: float) -> None:
        new_zoom = max(self.min_zoom, min(self.max_zoom, self.zoom + delta_zoom))
        if new_zoom == self.zoom:
            return

        target_lat, target_lon = self.screen_to_latlon(screen_x, screen_y)
        self.zoom = new_zoom

        target_gx, target_gy = self.latlon_to_pixel(target_lat, target_lon)
        new_cx = target_gx - (screen_x - (self.width / 2))
        new_cy = target_gy - (screen_y - (self.height / 2))

        self.center_lat, self.center_lon = self.pixel_to_latlon(new_cx, new_cy)
        self._fire_zoom_changed(self.zoom)

    # -----------------------------------------------------------------
    # Internal Event Handlers
    # -----------------------------------------------------------------
    def self__configure(self, event) -> None:
        self.width = event.width
        self.height = event.height
        self.draw()

    def self__mouse_click(self, event) -> None:
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def self__mouse_drag(self, event) -> None:
        dx = event.x - self._drag_start_x
        dy = event.y - self._drag_start_y

        self._drag_start_x = event.x
        self._drag_start_y = event.y

        self.pan_by_pixels(dx, dy)
        self.draw()

    def self__mouse_wheel(self, event) -> None:
        if event.delta > 0:
            self.self__zoom(1, event.x, event.y)
        elif event.delta < 0:
            self.self__zoom(-1, event.x, event.y)

    def self__zoom(self, delta: int, screen_x: float, screen_y: float) -> None:
        self.zoom_at_screen_pos(delta, screen_x, screen_y)
        self.draw()

    # -----------------------------------------------------------------
    # Tile Rendering Engine
    # -----------------------------------------------------------------
    def draw(self) -> None:
        """Draws base tiles. Derived classes should override this."""
        self._draw_tiles()
        self._draw_credits()

    def _draw_tiles(self) -> None:
        self.config(cursor=self.busy_cursor)
        self.delete("tile")  # delete all old tiles

        cx, cy = self.latlon_to_pixel(self.center_lat, self.center_lon)

        left_px = cx - (self.width / 2)
        right_px = cx + (self.width / 2)
        top_px = cy - (self.height / 2)
        bottom_px = cy + (self.height / 2)

        min_tx = int(math.floor(left_px / 256.0))
        max_tx = int(math.floor(right_px / 256.0))
        min_ty = int(math.floor(top_px / 256.0))
        max_ty = int(math.floor(bottom_px / 256.0))

        n_tiles = 2**self.zoom

        for tx in range(min_tx, max_tx + 1):
            for ty in range(min_ty, max_ty + 1):
                wrapped_tx = tx % n_tiles

                if ty < 0 or ty >= n_tiles:
                    continue

                tile_key = (self.zoom, wrapped_tx, ty)

                tile_x = (tx * 256) - cx + (self.width / 2)
                tile_y = (ty * 256) - cy + (self.height / 2)

                if tile_key in self.tile_cache:
                    # Draw the cached tile
                    self.create_image(tile_x, tile_y, image=self.tile_cache[tile_key], anchor="nw", tags=("tile"))
                else:
                    # Draw a blank tile while we fetch the actual tile
                    self.config(cursor=self.busy_cursor)
                    self.create_rectangle(tile_x, tile_y, tile_x + 256, tile_y + 256, outline="#2a2a2a", fill="#f0f0f0", tags=("tile"))
                    self._fetch_tile(tile_key)

        self.tag_lower("tile")
        self._draw_credits()
        self.config(cursor=self.default_cursor)

    def _draw_credits(self) -> None:
        """Draws the required map copyright credits in the bottom-right corner."""
        self.delete("credits")
        credits: str = self._get_credits()
        font_style = ("Segoe UI", 8)

        width = self.width
        height = self.height
        pad_x, pad_y = 3, 3
        x2 = width - 4
        y2 = height - 4

        # Approximate string size
        text_w = len(credits) * 6
        text_h = 12

        x1 = x2 - text_w - (pad_x * 2)
        y1 = y2 - text_h - (pad_y * 2)

        self.create_rectangle(x1, y1, x2, y2, fill="#e0e0e0", outline="#333333", width=1, tags=("credits"))
        self.create_text(x2 - pad_x, y2 - pad_y, text=credits, fill="black", font=font_style, anchor="se", tags=("credits"))

    def _get_credits(self) -> str:
        """Returns the map copyright credits. Derived classes must override this method to add their own credits."""
        return "Map: © OpenStreetMap"

    def _fetch_tile(self, tile_key) -> None:
        threading.Thread(target=self._fetch_tile_in_background, args=(tile_key,), daemon=True).start()

    def _fetch_tile_in_background(self, tile_key):
        zoom, x, y = tile_key
        url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MapControl/1.0 (mjackson@gmail.com)"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                img_data = resp.read()

            img = Image.open(io.BytesIO(img_data))
            self.tile_queue.put((tile_key, img))
        except Exception:
            pass

    def _poll_tile_queue(self) -> None:
        updated = False
        while True:
            try:
                tile_key, img = self.tile_queue.get_nowait()
                self.tile_cache[tile_key] = ImageTk.PhotoImage(img)
                updated = True
            except queue.Empty:
                self.config(cursor=self.default_cursor)
                break

        if updated:
            self._draw_tiles()
            tile_cache_size = len(self.tile_cache)
            if self.last_tile_cache_size_reported != tile_cache_size:
                self._fire_status_message(f"Map tiles downloaded: {tile_cache_size}")
                self.last_tile_cache_size_reported = tile_cache_size

        # Re-poll queue every 50ms
        self.after(50, self._poll_tile_queue)

    def _fire_zoom_changed(self, zoom: int) -> None:
        if self.on_zoom_changed:
            self.on_zoom_changed(zoom)

    def _fire_status_message(self, message: str) -> None:
        if self.on_status_message:
            self.on_status_message(message)


# =====================================================================
# Derived EarthquakeMap Class
# =====================================================================
class EarthquakeMap(MapControl):

    USGS_QUERY_API_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

    def __init__(self, master, width: int = 800, height: int = 600, **kwargs):
        super().__init__(master, width=width, height=height, **kwargs)

        self.earthquakes = []
        self.eq_item_map = {}  # item_id -> (eq_dict, screen_x, screen_y)
        self.selected_eq_data = None  # active earthquake tooltip
        self.earthquake_queue = queue.Queue()

        self.bind("<Button-1>", self._on_canvas_click, add="+")

        # Start non-blocking data queue polling loop
        self._poll_data_queue()

    def _fetch_earthquakes_in_background(self, target_url: str) -> None:
        try:
            req = urllib.request.Request(target_url, headers={"User-Agent": "PythonEarthquakeMap/1.0"})
            with urllib.request.urlopen(req, timeout=15) as response:
                raw_data = json.loads(response.read().decode("utf-8"))

            earthquakes = []
            for feature in raw_data.get("features", []):
                geometry = feature.get("geometry", {})
                properties = feature.get("properties", {})
                coords = geometry.get("coordinates", [])

                if len(coords) >= 2:
                    lon, lat = coords[0], coords[1]
                    depth = coords[2] if len(coords) > 2 else 0.0
                    mag = properties.get("mag") or 0.0

                    earthquakes.append(
                        {
                            "id": feature.get("id"),
                            "title": properties.get("title", "Unknown Event"),
                            "place": properties.get("place", "Unknown Location"),
                            "lat": lat,
                            "lon": lon,
                            "mag": mag,
                            "depth_km": depth,
                            "time_ms": properties.get("time"),
                        }
                    )

            self.earthquake_queue.put(earthquakes)
        except Exception as e:
            self.earthquake_queue.put([])

    def fetch_earthquakes(self, start_date: str, end_date: str, min_mag: float = 1.0) -> None:
        params = {
            "format": "geojson",
            "starttime": start_date,
            "endtime": end_date,
            "minmagnitude": str(min_mag),
            "eventtype": "earthquake",
        }
        target_url = f"{self.USGS_QUERY_API_URL}?{urllib.parse.urlencode(params)}"

        # Run the actual retrieval code on a background thread
        threading.Thread(target=self._fetch_earthquakes_in_background, args=(target_url,), daemon=True).start()

    def _poll_data_queue(self) -> None:
        try:
            self.earthquakes = self.earthquake_queue.get_nowait()
            self.selected_eq = None
            self.draw()
        except queue.Empty:
            pass

        self.after(100, self._poll_data_queue)

    @staticmethod
    def _get_magnitude_color(mag: float) -> str:
        if mag < 2.0:
            return "#2ecc71"
        elif mag < 3.5:
            return "#f1c40f"
        elif mag < 5.0:
            return "#e67e22"
        elif mag < 6.5:
            return "#e74c3c"
        else:
            return "#8e44ad"

    @staticmethod
    def _get_magnitude_radius(mag: float) -> float:
        clamped_mag = max(0.1, mag)
        radius = 2.5 * math.pow(1.45, clamped_mag)
        return max(3.0, min(radius, 45.0))

    def draw(self) -> None:
        """Overrides MapControl.redraw to render both tiles and earthquake overlays."""
        self._draw_tiles()
        self._draw_earthquakes()

    def _get_credits(self) -> str:
        """Overrides MapControl._get_credits to add earthquake credits."""
        return f"{super()._get_credits()}  |  Earthquake data: earthquake.usgs.gov"

    def _draw_earthquakes(self) -> None:
        """Draws earthquakes as colored circles."""
        self.delete("eq_marker")
        self.delete("tooltip")
        self.eq_item_map.clear()

        sorted_quakes = sorted(self.earthquakes, reverse=True, key=lambda q: q["mag"])

        for eq in sorted_quakes:
            wrapped_positions = self.get_screen_coords_wrapped(eq["lat"], eq["lon"])

            mag = eq["mag"]
            r = self._get_magnitude_radius(mag)  # radius
            color = self._get_magnitude_color(mag)

            for sx, sy in wrapped_positions:
                margin = 50
                if not (-margin <= sx <= self.width + margin and -margin <= sy <= self.height + margin):
                    continue  # not enough room to display earthquake

                item_id = self.create_oval(sx - r, sy - r, sx + r, sy + r, fill=color, outline="#000000", width=1, tags="eq_marker")
                self.eq_item_map[item_id] = (eq, sx, sy)

                if mag >= 3.5:
                    text_id = self.create_text(sx, sy, text=f"{mag:.1f}", fill="#ffffff", anchor="center", tags="eq_marker")
                    self.eq_item_map[text_id] = (eq, sx, sy)

        self.tag_raise("eq_marker")
        self.tag_raise("credits")

        if self.selected_eq_data:
            eq, _, _ = self.selected_eq_data
            wrapped = self.get_screen_coords_wrapped(eq["lat"], eq["lon"])
            if wrapped:
                center_x = self.width / 2
                best_sx, best_sy = min(wrapped, key=lambda p: abs(p[0] - center_x))
                self._draw_tooltip(eq, best_sx, best_sy)

    def get_screen_coords_wrapped(self, lat: float, lon: float) -> list[tuple[float, float]]:
        """Returns screen coordinates for an entity rendered across wrapped world instances."""
        world_width_px = (2.0**self.zoom) * 256.0
        gx, gy = self.latlon_to_pixel(lat, lon)
        cx, cy = self.latlon_to_pixel(self.center_lat, self.center_lon)

        base_x = (gx - cx) + (self.width / 2)
        screen_y = (gy - cy) + (self.height / 2)

        # Calculate visible world offsets (-2 to +2 full world widths)
        coords = []
        for offset in range(-2, 3):
            screen_x = base_x + (offset * world_width_px)
            if -100 <= screen_x <= self.width + 100:
                coords.append((screen_x, screen_y))

        return coords

    def _on_canvas_click(self, event) -> None:
        clicked_items = self.find_withtag("current")
        if not clicked_items:
            if self.selected_eq_data:
                self._hide_tooltip()
            return

        current_item = clicked_items[0]
        tags = self.gettags(current_item)

        if "eq_marker" in tags and current_item in self.eq_item_map:
            eq, sx, sy = self.eq_item_map[current_item]
            self.selected_eq_data = (eq, sx, sy)
            self._draw_tooltip(eq, sx, sy)
            return

        if "tooltip" in tags:
            return

        if self.selected_eq_data:
            self._hide_tooltip()

    def _draw_tooltip(self, eq: dict, sx: float, sy: float) -> None:
        """Draws a tooltip describing the selected earthquake."""
        self.delete("tooltip")

        radius = self._get_magnitude_radius(eq["mag"])

        place: str = f"{eq.get('place', 'Unknown')}"
        tooltip_w = max(220, len(place) * 7.5)
        tooltip_h = 105
        offset_x = radius + 10
        offset_y = -(tooltip_h / 2)

        x1 = sx + offset_x
        y1 = sy + offset_y
        x2 = x1 + tooltip_w
        y2 = y1 + tooltip_h

        # Choose screen location, to ensure the tooltip is fully visible

        if x2 > self.width - 10:
            x1 = sx - offset_x - tooltip_w
            x2 = x1 + tooltip_w

        if y1 < 10:
            y1 = 10
            y2 = y1 + tooltip_h
        elif y2 > self.height - 30:
            y2 = self.height - 30
            y1 = y2 - tooltip_h

        if eq.get("time_ms"):
            dt_utc = datetime.datetime.fromtimestamp(eq["time_ms"] / 1000.0, tz=datetime.timezone.utc)
            time_str = dt_utc.strftime("%Y-%m-%d %H:%M UTC")
        else:
            time_str = "N/A"

        header_color = self._get_magnitude_color(eq["mag"])

        # Tooltip panel
        self.create_rectangle(x1, y1, x2, y2, fill="white", outline="#333333", width=2, tags="tooltip")

        # Color banner
        self.create_rectangle(x1, y1, x2, y1 + 26, fill=header_color, outline="", tags="tooltip")

        # Header title
        header_color = "#ffffff" if eq["mag"] >= 5.0 else "#000000"
        header_text = f"M{eq['mag']:.1f} Earthquake"
        self.create_text(x1 + 8, y1 + 13, text=header_text, fill=header_color, font=("Segoe UI", 9, "bold"), anchor="w", tags="tooltip")

        # Close button
        button_font = font = ("Segoe UI", 10, "bold")
        close_id = self.create_text(x2 - 12, y1 + 13, text="✕", fill=header_color, font=button_font, anchor="center", tags="tooltip")
        self.tag_bind(close_id, "<Button-1>", lambda e: self._hide_tooltip())

        # Earthquake details
        lines = [
            f"Place: {place[:50]}",
            f"Time:  {time_str}",
            f"Coord: {eq['lat']:.3f}°, {eq['lon']:.3f}°",
            f"Depth: {eq['depth_km']:.1f} km",
        ]

        text_y = y1 + 38
        for line in lines:
            self.create_text(x1 + 8, text_y, text=line, fill="black", anchor="w", tags="tooltip")
            text_y += 18

        self.tag_raise("tooltip")  # ensure tooltip appears in front of other map elements

    def _hide_tooltip(self) -> None:
        self.selected_eq_data = None
        self.delete("tooltip")


# =====================================================================
# Main Application Window
# =====================================================================
class EarthquakeApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Earthquake Tracker")
        self.center_window(root, 950, 720)
        self.root.minsize(400, 300)

        today = datetime.datetime.now()
        week_ago = today - datetime.timedelta(days=7)

        # --------------- Top Control Bar -------------------------

        controlbar_color: str = "#F0F0F0"
        self.toolbar = tk.Frame(self.root, bg=controlbar_color, pady=5, padx=8, relief="ridge", borderwidth=1)
        self.toolbar.pack(fill="x", side="top")

        tk.Label(self.toolbar, text="Start:", bg=controlbar_color, fg="black").pack(side="left", padx=(0, 2))
        self.entry_start = tk.Entry(self.toolbar, width=11)
        self.entry_start.insert(0, week_ago.strftime("%Y-%m-%d"))
        self.entry_start.pack(side="left", padx=(0, 30))

        tk.Label(self.toolbar, text="End:", bg=controlbar_color, fg="black").pack(side="left", padx=(0, 2))
        self.entry_end = tk.Entry(self.toolbar, width=11)
        self.entry_end.insert(0, today.strftime("%Y-%m-%d"))
        self.entry_end.pack(side="left", padx=(0, 30))

        tk.Label(self.toolbar, text="Lowest Magnitude:", bg=controlbar_color, fg="black").pack(side="left", padx=(0, 2))
        self.entry_mag = tk.Entry(self.toolbar, width=4)
        self.entry_mag.insert(0, "3.0")
        self.entry_mag.pack(side="left", padx=(0, 50))

        self.btn_filter = tk.Button(
            self.toolbar,
            text="Get Earthquakes",
            command=self.get_earthquakes,
            bg="#fdfdfd",
            fg="#000000",
            relief="raised",
            padx=8,
        )
        self.btn_filter.pack(side="left", padx=4)

        self.root.bind("<Return>", lambda event: self.btn_filter.invoke())

        # ----------------- MapControl ------------------------------

        self.map_control = EarthquakeMap(self.root, width=450, height=300)
        self.map_control.pack(fill="both", expand=True)

        self._build_magnitude_legend()

        # -----------------  Status Bar ------------------------------

        self.statusbar = tk.Frame(self.root, bg=controlbar_color, height=24, relief="groove", borderwidth=1)
        self.statusbar.pack(fill="x", side="bottom")

        self.lbl_status_message = tk.Label(self.statusbar, text="", anchor="w", bg=controlbar_color, fg="black")
        self.lbl_status_message.pack(side="left", fill="x", expand=True, padx=10)

        self.lbl_zoom = tk.Label(self.statusbar, text="", width=15, anchor="e", bg=controlbar_color, fg="black")
        self.lbl_zoom.pack(side="right", padx=10)

        self.lbl_latlon = tk.Label(self.statusbar, text="", width=40, bg=controlbar_color, fg="black")
        self.lbl_latlon.pack(side="right", padx=10)

        # Set MapControl event handlers
        self.map_control.bind("<Motion>", self._map_control__mouse_event)
        self.map_control.bind("<B1-Motion>", self._map_control__mouse_event, add="+")
        self.map_control.on_status_message = self._map_control__status_message
        self.map_control.on_zoom_changed = self._map_control__zoom_changed

        self._map_control__zoom_changed(self.map_control.zoom)

        # Initial data fetch
        self.get_earthquakes()

    def center_window(self, root, width, height) -> None:
        # Set window size
        window_width = width
        window_height = height

        # Get screen dimensions
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()

        # Calculate centered position
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2

        # Set window size and position
        root.geometry(f"{window_width}x{window_height}+{x}+{y}")

    def _build_magnitude_legend(self) -> None:
        bg_color = "#111111"

        legend = tk.Frame(self.map_control, bg=bg_color, bd=1, relief="solid")
        legend.place(relx=0.01, rely=0.97, anchor="sw")

        title = tk.Label(legend, text="Magnitude Scale", bg=bg_color, fg="white", font=("Segoe UI", 8, "bold"))
        title.pack(anchor="w", padx=6, pady=(4, 2))

        tiers = [
            ("< 2.0", "#2ecc71"),
            ("2.0 - 3.4", "#f1c40f"),
            ("3.5 - 4.9", "#e67e22"),
            ("5.0 - 6.4", "#e74c3c"),
            ("≥ 6.5", "#8e44ad"),
        ]

        for label_text, color in tiers:
            row = tk.Frame(legend, bg=bg_color)
            row.pack(fill="x", padx=6, pady=1)

            swatch = tk.Canvas(row, width=10, height=10, bg=bg_color, highlightthickness=0)
            swatch.pack(side="left", padx=(0, 6))
            swatch.create_oval(1, 1, 9, 9, fill=color, outline="black")

            tk.Label(row, text=label_text, bg=bg_color, fg="#cccccc", font=("Consolas", 8)).pack(side="left")

    def get_earthquakes(self) -> None:
        start_str = self.entry_start.get().strip()
        end_str = self.entry_end.get().strip()
        try:
            min_mag = float(self.entry_mag.get().strip())
        except ValueError:
            min_mag = 1.0

        self.map_control.fetch_earthquakes(start_date=start_str, end_date=end_str, min_mag=min_mag)

    def _map_control__mouse_event(self, event) -> None:
        lat, lon = self.map_control.screen_to_latlon(event.x, event.y)
        self.lbl_latlon.config(text=f"Latitude: {lat:10.4f}°,   Longitude: {lon:10.4f}°")

    def _map_control__status_message(self, message: str) -> None:
        self.lbl_status_message.config(text=message)
        self.lbl_status_message.update_idletasks()

    def _map_control__zoom_changed(self, zoom: int) -> None:
        self.lbl_zoom.config(text=f"Zoom level: {zoom}")


if __name__ == "__main__":
    main_root = tk.Tk()
    app = EarthquakeApp(main_root)
    main_root.mainloop()
