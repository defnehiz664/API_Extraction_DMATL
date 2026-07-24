import requests

# Test: what phases does OQMD return for W-Re system?
url = "https://oqmd.org/oqmdapi/formationenergy"
params = {
    "element_set": "W,Re",
    "stability__lte": 0,      # only phases ON the hull (e_above_hull = 0)
    "fields": "name,spacegroup,delta_e,stability,prototype,unit_cell",
    "limit": 50
}

r = requests.get(url, params=params)
data = r.json()

print(f"Total phases found: {data['meta']['data_count']}")
print()
for entry in data['data']:
    print(entry)