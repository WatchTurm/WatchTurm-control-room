"""
Simple local web server for development.

Serves the web files so CORS doesn't block API requests.

Usage:
    python start_local_server.py [--port 8080]
"""

import argparse
import http.server
import socketserver
import os
from pathlib import Path

class CORSRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local web server for development")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port to serve on (default: 8080)")
    
    args = parser.parse_args()
    
    # Change to project root (parent of web directory) so data/ folder is accessible
    web_dir = Path(__file__).parent
    project_root = web_dir.parent
    os.chdir(project_root)
    
    PORT = args.port
    
    with socketserver.TCPServer(("", PORT), CORSRequestHandler) as httpd:
        print(f"Serving from project root: {project_root}")
        print(f"Open http://localhost:{PORT}/web/index.html in your browser")
        print("\nPress Ctrl+C to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped")
