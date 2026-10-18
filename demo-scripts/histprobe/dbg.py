import sys, re
from google.protobuf import descriptor_pb2
data = open(sys.argv[1], "rb").read()
name = sys.argv[2].encode()
for m in re.finditer(re.escape(name), data):
    s = m.start()
    print(s, data[s-3:s].hex(), data[s+len(name):s+len(name)+40])
