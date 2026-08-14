import json, copy
from pathlib import Path
from pydantic import ValidationError
from schema_loader import load_schema

model, _ = load_schema("schemas/copper/schema.yaml")
good = json.loads(Path("/Users/defnehiz/API_Extraction_DMATL-1/data/outputs/10-1016_j-ijfatigue-2016-07-019_extraction.json").read_text())["records"][0]

# 1. the real record should pass
try:
    model.model_validate(good); print("clean record: PASS (expected)")
except ValidationError as e:
    print("clean record FAILED (unexpected):", e)

# 2. inject a bogus field; this should now fail
bad = copy.deepcopy(good)
bad["totally_made_up_field"] = 123
try:
    model.model_validate(bad); print("broken record: PASS  <-- BAD, validator has no teeth")
except ValidationError:
    print("broken record: CAUGHT (expected)")