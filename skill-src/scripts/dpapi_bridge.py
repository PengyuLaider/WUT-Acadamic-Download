"""Private IPC helper: stdout is consumed only by the calling credential process."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_byte))]
def main():
    raw=Path(sys.argv[1]).read_bytes()
    buf=ctypes.create_string_buffer(raw)
    source=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_byte)));target=Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source),None,None,None,None,0,ctypes.byref(target)):
        raise RuntimeError('dpapi_decrypt_failed')
    try:sys.stdout.buffer.write(ctypes.string_at(target.data,target.size))
    finally:ctypes.windll.kernel32.LocalFree(target.data)
if __name__=='__main__':
    try:main()
    except Exception:sys.exit(1)
