import logging
from serial import Serial
import time
import os
import argparse
import threading
from websocket_server import WebsocketServer
import json
from http.server import HTTPServer, SimpleHTTPRequestHandler


class SignalingHTTPServer(HTTPServer):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ready_event = threading.Event()

    def service_actions(self):
        self.ready_event.set()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory='web', **kwargs)


arg_parser = argparse.ArgumentParser(description="Serial port reader",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
arg_parser.add_argument("--http-listen", help="HTTP server listen address", required=False, default="0.0.0.0")
arg_parser.add_argument("--http-port", help="HTTP server port", required=False, default=8000)

args = arg_parser.parse_args()


def find_serial_ports() -> list[str]:
    # Find all serial ports
    ports = [f"/dev/{i}" for i in os.listdir("/dev") if "ttyUSB" in i or "ttyACM" in i]
    return ports


def main():
    available_ports = find_serial_ports()
    connected_serials: list[Serial] = []

    def reset_esp32(s):
        s.reset_input_buffer()
        s.dtr = True
        s.rts = False
        time.sleep(0.1)
        s.dtr = False
        s.rts = True
        time.sleep(0.1)
        s.dtr = True

    def get_serial(port: str) -> Serial | None:
        for s in connected_serials:
            if s.is_open and s.port == port:
                return s
        return None

    def list_connected_ports():
        return json.dumps({
            "type": "list_connected",
            "ports": [s.port for s in connected_serials]
        })

    def list_available_ports():
        connected_ports = list_connected_ports()

        return json.dumps({
            "type": "list_available",
            "ports": [{'port': p, 'connected': p in connected_ports} for p in available_ports]
        })

    def new_client(client, _server):
        _server.send_message_to_all(list_available_ports())

    def message_received(client, _server, message):
        connected_ports = list_connected_ports()

        def success(t):
            t = t + '_response'
            _server.send_message(client, json.dumps({'success': True, 'type': t}))

        def failure(t, _message=None):
            t = t + '_response'
            _server.send_message(client, json.dumps({'success': False, 'type': t, message: _message}))

        try:
            parsed = json.loads(message)

            print(parsed)

            if parsed['type'] == 'reboot' and parsed['port'] in connected_ports:
                print("Rebooting")
                _server.send_message_to_all({'type': 'rebooting'})
                ser = get_serial(parsed['port'])
                reset_esp32(ser)
                _server.send_message_to_all(json.dumps({"type": "reboot_done", "port": parsed['port']}))
            elif parsed['type'] == 'sendmsg' and parsed['msg'] != '' and parsed['port'] in connected_ports:
                msg = parsed['msg']
                print(f"Sending message to device: {msg}")
                ser = get_serial(parsed['port'])
                ser.write(msg.encode())
                success(parsed['type'])
            elif parsed['type'] == 'connect' and parsed['baud'] != '' and parsed['port'] in available_ports:
                if parsed['port'] not in connected_ports:
                    baud = int(parsed['baud'])
                    ser = Serial(parsed['port'], baud, timeout=0.5)
                    connected_serials.append(ser)
                    _server.send_message(client, json.dumps({'type': 'connect_response', 'success': True, 'port': parsed['port']}))
                else:
                    _server.send_message(client, json.dumps({'type': 'connect_response', 'success': False, 'port': parsed['port']}))
            elif parsed['type'] == 'disconnect' and parsed['port'] in connected_ports:
                ser = get_serial(parsed['port'])
                ser.close()
                connected_serials.remove(ser)
                _server.send_message(client, json.dumps({'type': 'disconnect_response', 'success': True, 'port': parsed['port']}))
            elif parsed['type'] == 'list_connected':
                _server.send_message(client, list_connected_ports())
            elif parsed['type'] == 'list_available':
                _server.send_message(client, list_available_ports())
            else:
                print("Unknown message", parsed['type'])

        except json.decoder.JSONDecodeError:
            print("Failed to parse message")
            pass
        except Exception as ex:
            print(ex)

    server = WebsocketServer(host='0.0.0.0', port=13254, loglevel=logging.INFO)
    server.set_fn_new_client(new_client)
    server.set_fn_message_received(message_received)
    server.run_forever(threaded=True)

    def close_serials():
        for s in connected_serials:
            if s.is_open:
                s.close()

    try:
        print("Press Ctrl-C to quit.")

        while True:
            for s in connected_serials:
                try:
                    if s and s.is_open and s.in_waiting > 0:
                        line = s.readline().decode("utf-8").rstrip()
                        data = {
                            'type': 'message',
                            'message': line,
                            'port': s.port
                        }

                        server.send_message_to_all(json.dumps(data))
                except UnicodeDecodeError:
                    pass
                except Exception as e:
                    print(e)

            time.sleep(0.1)
    except KeyboardInterrupt:
        close_serials()
        print("Exiting")

    close_serials()


if __name__ == "__main__":
    web_listen = args.http_listen
    web_port = args.http_port

    with SignalingHTTPServer((web_listen, web_port), Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        httpd.ready_event.wait()

        main()

        httpd.shutdown()
