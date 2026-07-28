from mendeleev import element
w = element("Cu")
for attr in dir(w):
    if not attr.startswith("_"):
        try:
            print(f"{attr:45s} {getattr(w, attr)}")
        except:
            pass