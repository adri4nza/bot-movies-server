import http.server, socketserver, os, json
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            cap = open('/sys/class/power_supply/BAT1/capacity').read().strip()
            stat = open('/sys/class/power_supply/BAT1/status').read().strip()
        except:
            cap, stat = "0", "unknown"
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"battery": int(cap), "state": stat.lower()}).encode())
socketserver.TCPServer(("", 8086), Handler).serve_forever()