# Find serialized FileDescriptorProtos embedded in a binary; walk top-level fields to find the end.
import sys, re
from google.protobuf import descriptor_pb2
data = open(sys.argv[1], "rb").read()
def varint(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7f) << s; s += 7
        if x < 0x80: return r, i
def walk(start):
    i = start
    while i < len(data):
        try: k, j = varint(data, i)
        except IndexError: break
        f, w = k >> 3, k & 7
        if not (1 <= f <= 14) or w not in (0, 2) or (f == 1 and i != start): break
        if w == 0: _, j = varint(data, j)
        else:
            n, j = varint(data, j); j += n
        if j > len(data): break
        i = j
    return i
out = []
for m in re.finditer(rb"\n[\x01-\x7f]([A-Za-z0-9_./-]+\.proto)", data):
    if data[m.start()+1] != len(m.group(1)): continue
    end = walk(m.start())
    fd = descriptor_pb2.FileDescriptorProto()
    try: fd.ParseFromString(data[m.start():end])
    except Exception as e: continue
    if not fd.message_type and not fd.enum_type: continue
    out.append(fd)
seen = set()
fds = descriptor_pb2.FileDescriptorSet()
for fd in out:
    if fd.name in seen: continue
    seen.add(fd.name); fds.file.append(fd)
    pass  # print(fd.name, fd.package, "messages", len(fd.message_type), "enums", len(fd.enum_type))
open(sys.argv[2], "wb").write(fds.SerializeToString())
