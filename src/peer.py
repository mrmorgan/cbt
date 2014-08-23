import socket
import threading
import time

import convert
import node


class Peer(object):
    TIMEOUT = 2
    PROTOCOL = "BitTorrent protocol"

    def __init__(self):
        self.nodes = []
        self.handlers = {
            "on_recv": [],
            "on_recv_handshake": []
        }

    def append_node(self, ip, port):
        for n in self.nodes:
            if (n.ip, n.port) == (ip, port):
                return
        new_node = node.Node(ip, port)
        self.nodes.append(new_node)

    def on_recv(self, func):
        if func not in self.handlers["on_recv"]:
            self.handlers["on_recv"].append(func)

    def on_recv_handshake(self, func):
        if func not in self.handlers["on_recv_handshake"]:
            self.handlers["on_recv_handshake"].append(func)

    def connect_all(self):
        def connect(n):
            try:
                n.connect()
            except (socket.timeout, socket.error):
                n.close()
        threads = []
        for n in self.nodes:
            thread = threading.Thread(target=connect, args=(n,))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()

    def message(self):
        r = range(len(self.nodes))
        r.reverse()
        for i in r:
            if not self.nodes[i].conn:
                del self.nodes[i]
        for n in self.nodes:
            self._message_recv(n)
            self._message_send(n)
        time.sleep(0.05)

    def _message_send(self, n):
        try:
            outbox_len = len(n.outbox)
            elapsed = time.time() - n.last_send
            if not outbox_len and elapsed > 100:
                # Keep-alive message
                n.conn.send(convert.uint_chr(0))
                n.last_send = time.time()
            for x in xrange(outbox_len):
                chunk = n.outbox[0]
                n.conn.send(chunk)
                n.last_send = time.time()
                del n.outbox[0]
            n.outbox = []
        except socket.error:
            n.close()

    def _message_recv(self, n):
        try:
            chunk = n.conn.recv(node.Node.MAX_CHUNK_SIZE)
            if chunk:
                n.inbox.append(chunk)
                n.last_recv = time.time()
            # Check if I need to process a buffer
            if n.inbox.length and n.inbox.length != n.inbox.bad_length:

                # Make string from buffer
                buf = "".join(n.inbox.buf)

                # Check if a handshake
                pstr_len = ord(buf[0])
                if pstr_len == len(Peer.PROTOCOL):
                    # It's a handshake (or extremely huge message (about 318 MB), don't care for it)
                    if len(buf) < pstr_len + 49:
                        # A handshake is not received completely
                        n.inbox.bad()
                        return
                    # Call handshake handlers
                    handshake = buf[0:pstr_len+49]
                    for func in self.handlers["on_recv_handshake"]:
                        func(n, handshake)
                    # Clear a buffer and append remaining data if they are
                    n.inbox.clear()
                    if len(handshake) < len(buf):
                        n.inbox.append(buf[len(handshake):])
                    return

                # Other messages
                if len(buf) < 4:
                    # A message is not received completely
                    n.inbox.bad()
                    return
                m_len = convert.uint_ord(buf[0:4])
                if len(buf) < 4 + m_len:
                    # A message is not received completely
                    n.inbox.bad()
                    return
                # Call other messages handlers
                m_buf = buf[0:4+m_len]
                for func in self.handlers["on_recv"]:
                    func(n, m_buf)
                # Clear a buffer and append remaining data if they are
                n.inbox.clear()
                if len(m_buf) < len(buf):
                    n.inbox.append(buf[len(m_buf):])
        except socket.error as err:
            if err.errno != 10035:
                n.close()
