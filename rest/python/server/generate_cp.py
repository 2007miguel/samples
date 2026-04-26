from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import base64

def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

with open("merchant-pub.pem", "rb") as f:
    pub = serialization.load_pem_public_key(f.read())

nums = pub.public_numbers()
x = nums.x.to_bytes(32, "big")
y = nums.y.to_bytes(32, "big")

print("x =", b64u(x))
print("y =", b64u(y))