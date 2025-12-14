from __future__ import annotations

import json
from pathlib import Path

class JournalWatcher:
    __slots__ = ["firstRun", "lastJournalText", "lastCarrierRequest", "hasJumped", "departureTime", "lastFuel", "lastUsedFileName"]
    
    def __init__(self) -> None:
        self.reset_all()


    def reset_all(self) -> None:
        self.firstRun = True
        self.lastJournalText = ""
        self.lastCarrierRequest = ""
        self.hasJumped = False
        self.departureTime = ""
        self.lastFuel = 1000


    def process_journal(self, file_name) -> bool:
        self.lastUsedFileName = str(file_name)

        journal_path = Path(file_name)
        if not journal_path.exists():
            print(f"Journal file not found: {journal_path}")
            return False

        with journal_path.open("r", encoding="utf-8") as journal:
            journalText = journal.read()

        if journalText != self.lastJournalText and not self.firstRun:
            newText = journalText.replace(self.lastJournalText, "").strip()

            for line in newText.split("\n"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if event['event'] == "CarrierJumpRequest":
                    destination = event['SystemName']

                    if not self.firstRun:
                        self.lastCarrierRequest = destination
                        print("Carrier destination: " + destination)
                        self.departureTime = event['DepartureTime']
                        print("Departure time: " + self.departureTime)

                elif event['event'] == "CarrierStats":
                    fuel = event['FuelLevel']
                    print("Fuel: " + str(fuel)) 

                    if fuel < self.lastFuel and fuel < 100:
                        print("alert:Your Tritium is running low.")

                    self.lastFuel = fuel
                elif event['event'] == "CarrierJump":
                    self.hasJumped = True

            self.lastJournalText = journalText
        self.firstRun = False
        return True


    def last_carrier_request(self) -> str:
        self.process_journal(self.lastUsedFileName)
        return self.lastCarrierRequest


    def reset_jump(self) -> None:
        self.hasJumped = False


    def get_jumped(self) -> bool:
        self.process_journal(self.lastUsedFileName)
        return self.hasJumped
