# CTS (Carrier Traversal System)
The Traversal System is an Elite Dangerous fleet carrier auto-plotter, autojumper, and flight computer.

This is a refactored fork of [mck-9061/CATS](https://github.com/mck-9061/CATS).

## Traversal features
* Automatic jump plotting
* Supports personal and squadron carriers, including Drake-, Fortune-, Victory-, Nautilus-, and Javelin-class carriers
* Tritium restocking workflows for personal and squadron refuel modes
* Route time estimation and Discord webhook updates
* Simple GUI-free workflow that drives the Elite interface directly
* Adjusts for variable jump timers
* Imports routes from plain text
* Supports route import from [Spansh fleet carrier router](https://spansh.co.uk/fleet-carrier).
* Saves and resumes if interupted while traveling along the route. 

## Limitations
* This only works on Windows. A future port to Linux is possible, though highly unlikely.
* Odyssey is required; Horizons is not supported.
* The autopilot has experimental support for displays running at resolutions other than 1920x1080, though most resolutions haven't been tested.
* Elite Dangerous should be running on your primary monitor in fullscreen.
* Officially supported resolutions can be found in the `resolutions.md` file.
* Elite needs to be using the default keybinds - if you've got custom keybinds, or are using a controller or HOTAS, you should back up your binds then reset to default keyboard+mouse.

## Installation
* **Release build (recommended):** Download the latest zip from GitHub Releases. Extract it and keep everything in the extracted `TraversalSystem` folder together (exe plus data files).
* **From source:** Install Python + `requirements.txt`, then run `python TraversalSystem/main.py` or build with `build_TraversalSystem.sh`. 

## Updating
* **Release exe:** Download the .exe file from the release and replace the old one in your TraversalSystem folder. 
  * This is the recomended option unless otherwise noted in the release notes. 
* **Release zip:** Download the .zip file and copy your configuration files into the new TraversalSystem folder. 
  * The second option is necessary if noted in the release notes.

## Necessary files
Place/keep these files alongside the exe, whether running from release or from source:
* `settings.ini`
* The file with your route
* `res.csv`
* `photos.txt`
* `sequences/` folder

## Traversal system usage

### Configure the files
* `settings.ini` (all options in one file)
  * `webhook_url=` Discord webhook URL (leave blank to disable messages)
  * `journal_directory=` path to your Elite Dangerous journals (e.g. `~\Saved Games\Frontier Developments\Elite Dangerous\`)
  * `tritium_slot=` integer offset used when navigating cargo transfer for refuel. See section [Refueling Setup](#refueling-setup) below for instructions on how to set.
  * `route_file=` route file path; relative paths resolve next to `settings.ini` See section [Route Setup](#route-setup) below for instructions on how to set.
   * `route_position=` which entry to start from in the route file. `0` means start before the first line, `1` skips the first line, etc. (Overridden if a save file is present.)
  * `auto-plot-jumps=` true to let CATS plot jumps; false for manual prompts
  * `disable-refuel=` true to skip restocking
  * `auto-plot-jumps=` true to let CATS plot jumps; false for manual prompts
  * `disable-refuel=` true to skip restocking
  * `power-saving=` true to close/reopen the game between jumps (Steam only, highly experimental)
  * `refuel-mode=` 0 personal (first 8 items), 1 personal (after 8 items), 2 squadron
  * `single-discord-message=` true to edit one webhook message instead of posting new ones
  * `shutdown-on-complete=` true to power off when the route finishes
* Your route file (whatever you set in `route_file`): See section [Route Setup](#route-setup) below.

### Refueling Setup
Read this section carefully and follow the instructions, as refuelling needs to have the options set correctly in order to function.

#### Using a PERSONAL carrier
* Fill the carrier's tritium depot to full (1000 tritium).
* Use a ship with at least 200 cargo capacity.
* Fill your ship's cargo hold with tritium FROM your carrier.
* If this entry is in the first 8 items, i.e. you can reach it without pressing S:
  * In `settings.ini`, set `refuel-mode=0`
  * Count how many times you have to press W to get to that entry from the "Confirm Items Transfer" button.
  * Set `tritium_slot=` equal to that number.
* If the entry is not in the first 8 items:
    * In `settings.ini`, set `refuel-mode=1`
    * Back out of the transfer menu, then go back into it.
    * Press W, then count how many times you have to press S to get to that entry.
    * Set `tritium_slot=` equal to that number.

#### Using a SQUADRON carrier
* In `settings.ini`, set `refuel-mode=2`
* Fill the carrier's tritium depot to full (1000 tritium).
* Use a ship with at least 200 cargo capacity.
* Fill your ship's cargo hold with tritium.
* Go to the squadron bank menu and select the Commodities section.
* Hover over the top commodity in the list.
* Count how many times you have to press S to get to the tritium you want to use (if it's at the top, this would be 0).
* Set `tritium_slot=` equal to that number.

### Route Setup
Either download a .csv from the Spansh fleet carrier router (easiest). Or, put each system of your route on a new line in `route.txt` or any other .txt file.

Whichever option you choose, set `route_file` in `settings.ini` to the file name of the route. 

If needed, you can also set `route_position` to correspond to your current location along the route. If you are one jump away from the starting route system, set this value to 0. If you are at the first system on the route, set this value to 1, and so on. 

### Starting the route
* Dock with your carrier.
* Make sure your cursor is over the "Carrier Services" option, and that your internal panel (right) is on the home tab.
* Edit `settings.ini` with your journal directory, Discord webhook, tritium slot, route file location, and behaviour toggles.
* Make sure you've set up your [refueling configuration](#refueling-setup) and [route configuration](#route-setup) properly.
* Run the packaged `TraversalSystem.exe` (or `python TraversalSystem/main.py` from source), then tab to the Elite Dangerous window. It should now start to plot jumps.

### Resuming the route
If the traversal system is exited for any reason before the route ends (Ctrl+C, unhandled exception), a save file will be created that saves your current location along the route. You can simply reopen the .exe to resume the route. The value in `save.txt` will overwrite any value in `route_position`. 

## Traversal system disclaimer
Use of programs like this is technically against Frontier's TOS. While they haven't yet banned people for automating carrier jumps, the developer does not take any responsibility for any actions that could be taken against your account. Use at your own risk!

## Legal

The source code is released under the [MIT License](https://github.com/congenial-acorn/CATS/blob/main/LICENSE). Neither the tool or the developer is associated with or endorsed by Frontier Developments, Elite Dangerous, or any other member or tool of the Elite Dangerous community.
