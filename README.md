# EarthquakeTracker_Python
A Python desktop app that shows current and historic earthquakes around the world.

## Features

- Great project for getting started with maps and online earthquake tracking services.
- Shows earthquake activity worldwide.
- Uses the online USGS Earthquake API.
- Uses OpenStreetMap for maps.
- Relies on a single imported package: Pillow, used for image manipulation.

The following figure shows EarthquakeTracker in action.

<img width="952" height="752" alt="image" src="https://github.com/user-attachments/assets/918fe55b-27c1-4e84-85ab-dca72a5b8e5e" />

*Figure 1* - The app showing earthquake locations and magnitudes in Central America. 

The starting and end dates default to covering the last seven days. The app queries the USGS (United States Geological Survey) online service at ```earthquake.usgs.gov``` to get a list of events in the given period and matching the **Lowest Magnitude** filter. Earthquakes are denoted on the map by circles, colored according to the quake magnitude. 

## The UI Layout

The user interface uses Tkinter packing to achieve its layout, as shown in the following figure.

<img width="593" height="426" alt="image" src="https://github.com/user-attachments/assets/33eea9fe-efe2-4736-bb81-f94b021caeba" />

*Figure 2* - The packed Tkinter elements in the user interface.

## How it Works
The main window of the app is just used to setup the layout and handle user interactions with the UI controls. The interesting stuff is mostly in EarthquakeMap, derived from MapControl. MapControl handles the download and display of the map, built using OpenStreetMap tiles. EarthquakeMap handles the download and display of earthquake activity. The following figure shows the class hierarchy of EarthquakeMap.

<img width="196" height="502" alt="image" src="https://github.com/user-attachments/assets/41c8c118-c2da-48a7-b8f6-18646f69cea4" />

*Figure 3* - The class hierarchy of EarthquakeMap.

The map is made up by tiles that fit tightly together to create a complete and seamless image. The geographic area covered by each tile depends on the Zoom level, which you can change using the mouse wheel. You can also pan the map by clicking and dragging the mouse.
<p></p>
The map is initialized in EarthquakeApp with the following code fragment.

```python
class EarthquakeApp:

    def __init__(self, root):
        #...

        latitude = 36.7783  # California center
        longitude = -119.4179

        self.map_control = EarthquakeMap(self.root, latitude=latitude, longitude=longitude, width=450, height=300)
        self.map_control.pack(fill="both", expand=True)

        #...

        # Set MapControl event handlers
        self.map_control.bind("<Motion>", self._map_control__mouse_event)
        self.map_control.bind("<B1-Motion>", self._map_control__mouse_event, add="+")
        self.map_control.on_status_message = self._map_control__status_message
        self.map_control.on_zoom_changed = self._map_control__zoom_changed

        self._map_control__zoom_changed(self.map_control.zoom)  # to initialize the zoom level on the status bar

        # Initial data fetch
        self.map_control.fetch_earthquakes(start_date=start_str, end_date=end_str, min_mag=min_mag)

```
*Listing 1* - Initializing the map.

The map is centered on latitude = 36.7 and longitude = -119.4, with a zoom level that defaults to 5. These values make the map show California at startup time. Regardless of where the map starts up, you can always zoom and pan to any other place on the globe. 
<p>
Apart from the creation of the various Tkinter UI elements, EarthquakeApp contains logic mostly for handling user clicks and mouse actions. When you drag the map, MapControl downloads tiles for new areas being shown. When you zoom the map, MapControl downloads new tiles for the new zoom level. Tiles are 256 x 256 pixel images stored in a memory cache, to obviate the need to download a given tile more than once.

## The Map

Displaying a map is more complicated than it might seem at first, because the Earth is round and we usually display maps on a flat surface. A rather naive but simple way to handle the problem is to use a Mercator projection of the Earth. The idea is to conceptually wrap a cylinder around the Earth so that it touches the globe at the equator. Mercator maps are the projection of the Earth's surface onto that cylinder, which is then unwrapped into a flat rectangle. The following figure shows the idea as described by ChatGPT.

<img width="622" height="491" alt="image" src="https://github.com/user-attachments/assets/e6560249-4a0b-47a5-962e-dd078b441d89" />

*Figure 4* - Generating a Mercator projection of the Earth.

Mercator projections provide reasonably good approximations of countries at lower latitudes. The farther you get from the equator, the greater the distortion. Near the poles, the distortion is huge. Look at you big Greenland and Antarctica appear on the map. What makes Mercator projections popular is their ease of use. You can simply divide the square map into smaller square tiles, avoiding a lot of tricky math to account for the round Earth.
<br><br>
The *Web Mercator* is a special format for Mercator maps. It's based on a standard called EPSG:3857, which uses 256x256 pixel tiles. The coordinate system uses longitude for the X axis and latitude for the Y axis. The origin corresponds to the point at longitude=0 and latitude=0, which is the center of the map. This point turns out to be somewhere in the Gulf of Guinea, off the coast of West Africa. The X axis increases to the right (East). The Y axis increases upward (North).
In Web Mercator, the Earth is assumed to be a perfect sphere, so the height and width of the map are the same: about 40,000 km, which is the circumference of the Earth at the equator. Since the origin is in the middle of the map, no location can be farther than about + or - 20,000 km.

## Downloading and displaying the Map

You might think that retrieving and showing map tiles is a simple two-step process. For each tile in the map, do this:

1. Download tile.
2. Display tile.

Unfortunately life isn't so simple because the download process takes time and you don't want to lock up the UI while this in progress. The solution is to use a background thread for the download. This introduces another problem: when the background thread is finished downloading, you can't just call a Display method to render the tiles. Why? Because the app uses Tkinter for the display elements, and Tkinter doesn't allow its UI objects to be updated from a background thread. So we need a way for the background thread to somehow sync up with the UI thread. To achieve this, EarthquakeMap uses a producer-consumer pattern coordinated via a queue. See the following sequence diagram.

<img width="740" height="600" alt="image" src="https://github.com/user-attachments/assets/a166facb-2582-430b-b704-48d7d5de9095" />

*Figure 5* - Using a queue to pass tiles from a background thread to the UI thread.

In the figure, EarthquakeMap is depicted as two separate objects:

1. EarthquakeMap_UI: the UI thread *consumer* code.
2. EarthquakeMap_Background: the background thread *producer* code.

The Queue class is thread-safe, to you can add items from one thread and retrieve them from another. The following listings show fragments of each major method involved.

```python
# UI thread
def _draw_tiles(self) -> None:
  #...
	
  for x in range(min_x, max_x + 1):
	
    for y in range(min_y, max_y + 1):

      tile_key = (self.zoom, wrapped_tx, ty)    # tile_key is a tuple
      tile_x = ...
      tile_y = ...
 
      if tile_key in self.tile_cache:
        # Draw the cached tile
        self.create_image(tile_x, tile_y, image=self.tile_cache[tile_key], anchor="nw", tags=("tile"))
      else:
        # Draw a blank tile while we fetch the actual tile
        self.create_rectangle(tile_x, tile_y, tile_x + 256, tile_y + 256, outline="#2a2a2a", fill="#f0f0f0", tags=("tile"))
        self._fetch_tile(tile_key)

def _fetch_tile(self, tile_key) -> None:
  # start up background thread
  threading.Thread(target=self._fetch_tile_in_background, args=(tile_key,), daemon=True).start()



# *** Background thread ***
def _fetch_tile_in_background(self, tile_key):

  zoom, x, y = tile_key  # tile_key is a tuple
  url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"

  # download the tile data
  req = urllib.request.Request(url, headers={"User-Agent": "MapControl/1.0 (mjackson@gmail.com)"})
  with urllib.request.urlopen(req, timeout=5) as resp:
    img_data = resp.read()

  # convert to bitmap and save in queue
  img = Image.open(io.BytesIO(img_data))
  self.tile_queue.put((tile_key, img))
```
*Listing 2* - The producer code to download tiles on a background thread.
<br><br>

```python
# UI thread
def _poll_tile_queue(self) -> None:
  updated = False
  while True:
    try:
      tile_key, img = self.tile_queue.get_nowait()
      self.tile_cache[tile_key] = ImageTk.PhotoImage(img)  # save downloaded tile image
      updated = True
    except queue.Empty:
      self.config(cursor=self.default_cursor)
      break  # nothing left in queue

  if updated:
  self._draw_tiles()

  # poll queue again every 50ms
  self.after(50, self._poll_tile_queue)
```
*Listing 3* - The consumer code to display tiles on the UI thread.

## Downloading and Displaying Earthquakes

Earthquake data is downloaded from USGS and displayed as colored circles on the map. To avoid locking up the UI thread while the download is in progress, we use a queue again in a producer-consumer pattern like the one just described. See the following sequence diagram.

<img width="795" height="458" alt="image" src="https://github.com/user-attachments/assets/b03d5097-2654-4f29-bc3e-20694c64ea64" />

*Figure 6* - Using a queue to pass earthquake data from a background thread to the UI thread.

```python
# UI thread
def fetch_earthquakes(self, start_date: str, end_date: str, min_mag: float = 1.0) -> None:

  #...
  target_url = ... # usgs URI with start/end dates

  # Run the actual retrieval code on a background thread
  threading.Thread(target=self._fetch_earthquakes_in_background, args=(target_url,), daemon=True).start()



# *** Background thread ***
def _fetch_earthquakes_in_background(self, target_url: str) -> None:

  # download data in json format
  req = urllib.request.Request(target_url, headers={"User-Agent": "PythonEarthquakeMap/1.0"})
  with urllib.request.urlopen(req, timeout=15) as response:
    raw_data = json.loads(response.read().decode("utf-8"))

  earthquakes = []
	
  # parse json data and save in earthquakes list...

  self.earthquake_queue.put(earthquakes)

```
*Listing 4* - The producer code to download earthquake data on a background thread.
<br><br>

```python
# UI thread
def _poll_data_queue(self) -> None:
  try:
    self.earthquakes = self.earthquake_queue.get_nowait()
    self.selected_eq = None
    self.draw()  # display the data
  except queue.Empty:
    pass         # no more data in queue

  // check again in 100 ms
  self.after(100, self._poll_data_queue)
```
*Listing 5* - The consumer code to display earthquake data on the UI thread.

The ```EarthquakeMap.draw``` method then displays each earthquake on the map, as shown in the following listing.

```python
def draw(self) -> None:
  """Overrides MapControl.redraw to render both tiles and earthquake overlays."""
  self._draw_tiles()  # calls base class to display the map
  self._draw_earthquakes()

def _draw_earthquakes(self) -> None:
  """Draws earthquakes as colored circles."""
  self.delete("eq_marker")
  self.delete("tooltip")
  self.eq_item_map.clear()

  sorted_quakes: list = sorted(self.earthquakes, reverse=True, key=lambda q: q["mag"])

  for eq in sorted_quakes:
    # wrap eastmost tiles around to the west, so we don't "fall off" the map
    wrapped_positions = self.get_screen_coords_wrapped(eq["lat"], eq["lon"])

    magnitude = eq["mag"]
    r = self._get_magnitude_radius(magnitude)  # radius
    color = self._get_magnitude_color(magnitude)

    for sx, sy in wrapped_positions:
      item_id = self.create_oval(sx - r, sy - r, sx + r, sy + r, fill=color, outline="#000000", width=1, tags="eq_marker")
      self.eq_item_map[item_id] = (eq, sx, sy)

      text_id = self.create_text(sx, sy, text=f"{magnitude:.1f}", fill="#ffffff", anchor="center", tags="eq_marker")
      self.eq_item_map[text_id] = (eq, sx, sy)

  self.tag_raise("eq_marker")
  self.tag_raise("credits")

  if self.selected_eq_data:
	  # compute <tooltip x> and <tooltip y>...

    self._draw_tooltip(eq, <tooltip x>, <tooltip y>)
```
*Listing 6* - Displaying earthquakes as colored circles on the map.

## Some Famous Historical Earthquakes

Before closing I thought it might be interesting to show some major earthquakes from the past.
<br>

It was the year 1960 when Chile was struck by a magnitude 9.5 earthquake, the biggest in recorded history anywhere in the world.

<img width="952" height="752" alt="image" src="https://github.com/user-attachments/assets/5a1d2e67-9d2f-4355-ade0-fd6e8eaf1c63" />

*Figure 7* - The 1960 Valdivia earthquake in Chile.

Despite the tremendous power of the quake and the ensuing tsunami, the number of fatalities wasn't massive, possibly due to the area not having a large population at the time.
<p></p>
A few years before the Chile earthquake there was a magnitude 7.8 quake in Alaska. Although the 1958 quake wasn't truly massive, it produced an avalanche into nearby Lituya Bay, generating the largest mega tsunami in recorded history. Due to the peculiar shape and depth of the bay, the wave reached an astonishing 524 meters in height.

<img width="952" height="752" alt="image" src="https://github.com/user-attachments/assets/ee3998b5-7a95-4b9d-bc00-a612ea759233" />

*Figure 8* - The 1958 Alaska earthquake that generated the Lituya Bay mega tsunami.

The deadliest earthquake in the 20th century, and possibly of all time, was the 1976 Tangshan quake in China. 

<img width="952" height="752" alt="image" src="https://github.com/user-attachments/assets/4f0340e6-20a1-4825-bf48-96ad9a391c7d" />

*Figure 9* - The 1976 Tangshan earthquake in China.

Although the Tangshan magnitude was *only* 7.5,  the official death toll is 242,000. Unofficial foreign estimates have been as high as 800,000.

Another earthquake that caused massive fatalities were the 2004 Sumatra-Andaman earthquakes, which occurred between the 25th and 26th of December.

<img width="952" height="752" alt="image" src="https://github.com/user-attachments/assets/18eaf64b-833c-4944-a7a6-166aeebedb7b" />

*Figure 10* - The 2004 Sumatra-Andaman earthquake in Indonesia.

 The quake caused many fatalities, but its main legacy was the tsunami that followed. It was a truly world-level event, killing over 227,000 people across 14 countries. It was the deadliest (but not biggest) mega tsunami in history. An estimated 100,000+ fatalities occurred in Banda Aceh alone, a town on the Northwest tip of Sumatra (Indonesia). A 30-meter tsunami swept through the densely populated area, reaching over 5 km inland.

Although North America doesn't have a lot of catastrophic earthquakes (outside Alaska), 1906 was a pretty rough year, as shown in the next figure.

<img width="952" height="752" alt="image" src="https://github.com/user-attachments/assets/421607c7-2f1d-46bd-99a3-5dadc66bc506" />

*Figure 11* - The year 1906 in North America.

As you can see, the San Francisco earthquake wasn't the only significant event in 1906.

## Closing Notes
Much of the logic in a mapping app is devoted to coordinate management and conversions. If you don't understand the coordinate systems, you'll have a hard time modifying the code to add your own new features. 
<p><p>
In a Python app using Tkinter (or other GUI libraries), you have to take care how you update the UI when using background threads to retrieve mapping data. Queues are one solution but there are others.
