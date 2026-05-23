import sys
import time

try:
    time.sleep(1)
    print("test")
except Exception as e:
    with open("error.log", "w") as f:
        f.write(str(type(e)) + ": " + str(e))
