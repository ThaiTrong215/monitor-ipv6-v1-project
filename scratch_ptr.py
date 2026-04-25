import socket

def test_ptr(ip):
    try:
        hostname, aliaslist, ipaddrlist = socket.gethostbyaddr(ip)
        print(f"IP: {ip} -> Hostname: {hostname}")
    except Exception as e:
        print(f"IP: {ip} -> Error: {e}")

test_ptr("8.8.8.8")
test_ptr("192.168.2.2")
test_ptr("2001:4860:4860::8888")
