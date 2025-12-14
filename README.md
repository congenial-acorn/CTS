# CATS (Carrier Administration and Traversal System)
The Traversal System is an Elite Dangerous fleet carrier auto-plotter, autojumper, and flight computer.
Everything in this repo now targets that single purpose.

## Traversal features
* Automatic jump plotting (or manual prompts if you prefer)
* Supports personal and squadron carriers, including Drake-, Fortune-, Victory-, Nautilus-, and Javelin-class carriers
* Tritium restocking workflows for personal and squadron refuel modes
* Route time estimation and Discord webhook updates
* Simple GUI-free workflow that drives the Elite interface directly
* Imports routes from plain text; timings stay accurate even when jumps take longer than expected

## Limitations
* This only works on Windows and probably won't be ported to anything else.
* Odyssey is required; Horizons is not supported.
* The autopilot has experimental support for displays running at resolutions other than 1920x1080, though most resolutions haven't been tested.
* Elite Dangerous should be running on your primary monitor in fullscreen.
* Officially supported resolutions can be found in the `resolutions.md` file.
* Elite needs to be using the default keybinds - if you've got custom keybinds, or are using a controller or HOTAS, you should back up your binds then reset to default keyboard+mouse.

## Installation
* Install Python and the dependencies in `requirements.txt`.
* Configure `TraversalSystem/settings.txt` and `TraversalSystem/settings.ini` (see the comments in those files).
* Run `TraversalSystem/main.py` directly or package it with `build_TraversalSystem.sh` (PyInstaller).

## Traversal system usage
### Refuelling setup
Read this section carefully and follow the instructions, as refuelling needs to have the options set correctly in order to function.

### Important note: 16th October Type-11 Prospector Update 2
#### FDev have indicated that fixes to transferring commodities between ships and carriers will be coming; this is likely to break refuelling. I will be testing and updating this when the update releases.

#### Using a PERSONAL carrier
* Fill the carrier's tritium depot to full (1000 tritium).
* Use a ship with at least 200 cargo capacity.
* Fill your ship's cargo hold with tritium FROM your carrier.
* In the cargo transfer menu on your carrier, there will be 2 entries for tritium. Locate the entry with tritium in the CARRIER, not in the ship.
* If this entry is in the first 8 items, i.e. you can reach it without pressing S:
  * In the CATS options, set the "Refuelling mode" to "Personal (First 8 items)".
  * Count how many times you have to press W to get to that entry from the "Confirm Items Transfer" button.
  * Enter this number in the "Tritium slot" option in CATS.
* If the entry is not in the first 8 items:
    * In the CATS options, set the "Refuelling mode" to "Personal (After 8 items)".
    * Back out of the transfer menu, then go back into it.
    * Press W, then count how many times you have to press S to get to that entry.
    * Enter this number in the "Tritium slot" option in CATS.

#### Using a SQUADRON carrier
* In the CATS options, set the "Refuelling mode" to "Squadron".
* Fill the carrier's tritium depot to full (1000 tritium).
* Use a ship with at least 200 cargo capacity.
* Fill your ship's cargo hold with tritium.
* Go to the squadron bank menu and select the Commodities section.
* Hover over the top commodity in the list.
* Count how many times you have to press S to get to the tritium you want to use (if it's at the top, this would be 0).
* Enter this number in the "Tritium slot" option in CATS.

### Starting the route
* Dock with your carrier.
* Make sure your cursor is over the "Carrier Services" option, and that your internal panel (right) is on the home tab.
* Update `TraversalSystem/settings.txt` with your journal directory, Discord webhook, tritium slot, and route file location.
* Toggle behaviour in `TraversalSystem/settings.ini` (auto-plot, disable-refuel, power-saving, refuel-mode, and single-discord-message).
* Put each system on a new line in `route.txt` (or any file referenced by `route_file` in `settings.txt`).
* Run `python TraversalSystem/main.py`, then tab to the Elite Dangerous window. It should now start to plot jumps.

## Traversal system disclaimer
Use of programs like this is technically against Frontier's TOS. While they haven't yet banned people for automating carrier jumps, the developer does not take any responsibility for any actions that could be taken against your account.

<br><br>
o7
