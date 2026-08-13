from pathlib import Path
from xml.etree import ElementTree as ET

xml_path = next(Path("data/papers").glob("*.xml"))
print("XML:", xml_path.name)

def local(tag):                      # strip namespace, match by local name
    return tag.rsplit("}", 1)[-1]

root = ET.parse(xml_path).getroot()
tables = [e for e in root.iter() if local(e.tag) == "table"]
print(f"Found {len(tables)} <table> element(s)\n")

for ti, table in enumerate(tables, 1):
    print(f"===== TABLE {ti} =====")
    for row in (e for e in table.iter() if local(e.tag) == "row"):
        cells = []
        for entry in (e for e in row.iter() if local(e.tag) == "entry"):
            cells.append(" ".join(t.strip() for t in entry.itertext() if t.strip()))
        print(" | ".join(cells))
    print()