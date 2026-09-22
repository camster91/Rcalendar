"""Probe: does a second SO_REUSEADDR bind to 127.0.0.1:PORT succeed on Windows?

Mimics what socketserver/werkzeug do, then tries the same bind a second time.
"""
import socket
import sys

PORT = 18765


def bind_reuse():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", PORT))
    s.listen(5)
    return s


first = bind_reuse()
print("first bind ok, listening on", first.getsockname())

try:
    second = bind_reuse()
    print("SECOND BIND SUCCEEDED ->", second.getsockname())
    second_ok = True
except OSError as exc:
    print("second bind FAILED ->", type(exc).__name__, exc)
    second_ok = False

# Does a plain connect still reach somebody?
c = socket.socket()
c.settimeout(1.0)
print("connect_ex to port:", c.connect_ex(("127.0.0.1", PORT)))
c.close()
first.close()
if second_ok:
    second.close()
sys.exit(0)
