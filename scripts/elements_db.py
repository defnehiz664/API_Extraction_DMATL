from mendeleev import element
w = element("Re")
for attr in dir(w):
    if not attr.startswith("_"):
        try:
            print(f"{attr:45s} {getattr(w, attr)}")
        except:
            pass