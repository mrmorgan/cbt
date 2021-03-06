import hashlib
import math
import os
import socket
import time

import bcode
import convert
import downloader
import file
import node
import piece
import peer
import tracker
import version
import writer

collected = []


def collect(cls):
    """Decorator that collect all created Torrent objects
    to call their message() methods in main loop.

    """
    class Collector(cls):
        def __init__(self, *args, **kwargs):
            super(Collector, self).__init__(*args, **kwargs)
            collected.append(self)
    return Collector


def gen_id():
    """Generate an unique 20-byte string to identification
    a client in BitTorrent network. The first 8 bytes
    indicates type and version of a client and next
    12 bytes are random. Random 12-byte string generates
    from SHA1-hash of concatenation of main process ID
    and current time.

    """
    pid = os.getpid()
    timestamp = time.time()
    unique_str = "".join((
        str(pid),
        str(timestamp)
    ))
    hash = hashlib.sha1(unique_str).digest()
    id = "".join((
        version.CLIENT_IDENTIFIER,
        hash[len(version.CLIENT_IDENTIFIER):]
    ))
    return id


def gen_port(start=6881, end=6890):
    """Return the first unused port in the range
    or raise socket.error if there is no one.

    """
    for port in xrange(start, end):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        if result != 0:
            return port
    raise socket.error("Unable to listen any BitTorrent port")


def main_loop():
    SHOW_PROGRESS_EVERY = 2

    ts = time.time()
    try:
        while True:
            wait = True
            for obj in collected:
                result = obj.message()
                if not result:
                    wait = True
                if time.time() - ts >= SHOW_PROGRESS_EVERY:
                    print obj
                    ts = time.time()
            if wait:
                time.sleep(0.001)
    except KeyboardInterrupt:
        pass


@collect
class Torrent(object):
    MESSAGE_CHOKE = 0
    MESSAGE_UNCHOKE = 1
    MESSAGE_INTERESTED = 2
    MESSAGE_NOTINTERESTED = 3
    MESSAGE_HAVE = 4
    MESSAGE_BITFIELD = 5
    MESSAGE_REQUEST = 6
    MESSAGE_PIECE = 7
    MESSAGE_CANCEL = 8

    RECONNECT_AFTER = 30

    id = None
    port = None

    def __init__(self, torrent_path, download_path):
        if not Torrent.id:
            Torrent.id = gen_id()
        if not Torrent.port:
            Torrent.port = gen_port()

        # Attributes declaration
        self.download_path = download_path
        self.downloader = None
        self.hash = ""
        self.meta = {}
        self.peer = peer.Peer()
        self.pieces = []
        self.torrent_path = torrent_path
        self.tracker = None
        self.writer = writer.Writer()

        # Load meta data from .torrent
        with open(self.torrent_path, "rb") as f:
            self.meta = bcode.decode(f.read())
        self.hash = hashlib.sha1(bcode.encode(self.meta["info"])).digest()

        # Load pieces info
        piece_count = len(self.meta["info"]["pieces"]) / 20
        piece_length = self.meta["info"]["piece length"]
        for x in xrange(piece_count):
            hash = self.meta["info"]["pieces"][x*20:x*20+20]
            p = piece.Piece(hash, piece_length, len(self.pieces))
            self.pieces.append(p)

        # Init downloader
        self.downloader = downloader.Downloader(self.peer.nodes, self.pieces)

        # Load files info
        # Multifile mode
        offset = 0
        if "files" in self.meta["info"]:
            if self.download_path[-1] != os.sep:
                self.download_path += os.sep
            self.download_path += self.meta["info"]["name"]
            for file_info in self.meta["info"]["files"]:
                f = file.File(
                    intorrent_path=file_info["path"],
                    download_path=self.download_path,
                    size=file_info["length"],
                    offset=offset
                )
                offset += file_info["length"]
                self.writer.append_file(f)
        # Singlefile mode
        if "name" and "length" in self.meta["info"]:
            f = file.File(
                intorrent_path=self.meta["info"]["name"],
                download_path=self.download_path,
                size=self.meta["info"]["length"],
                offset=0
            )
            self.writer.append_file(f)

        # Load trackers list and select available
        trackers = []
        if "announce" in self.meta:
            trackers.append(self.meta["announce"])
        if "announce-list" in self.meta:
            for item in self.meta["announce-list"]:
                trackers.append(item[0])
        self.tracker = tracker.get(trackers)

        # Events handlers
        self.peer.on_recv(self.handle_message)
        self.peer.on_recv_handshake(self.handle_message_handshake)
        self.downloader.event_connect("piece", self.on_piece)
        self.downloader.event_connect("cancel", self.on_cancel)

    def __str__(self):
        return self._to_string()

    def start(self):
        self.writer.create_files()
        response = self.tracker.request(
            hash=self.hash,
            id=Torrent.id,
            port=Torrent.port,
            uploaded=0,
            downloaded=0,
            left=0,
            event="started"
        )
        if type(response["peers"]) is str:
            for x in xrange(0, len(response["peers"]), 6):
                ip = ".".join((str(ord(byte)) for byte in response["peers"][x:x+4]))
                port = convert.uint_ord(response["peers"][x+4:x+6])
                self.peer.append_node(ip, port)
        self.peer.connect_all()
        for n in self.peer.nodes:
            self.send_message_handshake(n)

    def stop(self):
        self.tracker.request(
            hash=self.hash,
            id=Torrent.id,
            port=Torrent.port,
            uploaded=0,
            downloaded=0,
            left=0,
            event="stopped"
        )

    def message(self):
        self.peer.message()
        self.downloader.message()

    def download_chunks(self):
        for request in self.downloader.next():
            if request.node.p_choke == node.Node.TRUE:
                self.send_message_unchoke(request.node)
                self.send_message_interested(request.node)
                request.node.wait_for_unchoke()
                request.node.p_choke = node.Node.WAITING
            self.send_message_request(request.node, request.piece, request.chunk * piece.Piece.CHUNK, piece.Piece.CHUNK)

    def handle_message(self, n, buf):
        if not n.handshaked:
            # Invalid peer
            n.close()
            return

        if len(buf) == 4 and convert.uint_ord(buf) == 0:
            # Keep-alive message
            return

        # Other messages
        m_type = ord(buf[4])
        if m_type == Torrent.MESSAGE_CHOKE:
            self.handle_message_choke(n)
        elif m_type == Torrent.MESSAGE_UNCHOKE:
            self.handle_message_unchoke(n)
        elif m_type == Torrent.MESSAGE_INTERESTED:
            self.handle_message_interested(n)
        elif m_type == Torrent.MESSAGE_NOTINTERESTED:
            self.handle_message_notinterested(n)
        elif m_type == Torrent.MESSAGE_HAVE:
            self.handle_message_have(n, buf[5:])
        elif m_type == Torrent.MESSAGE_BITFIELD:
            self.handle_message_bitfield(n, buf[5:])
        elif m_type == Torrent.MESSAGE_PIECE:
            self.handle_message_piece(n, buf[5:])

    def handle_message_handshake(self, n, buf):
        if n.handshaked:
            n.close()
            return
        pstr_len = ord(buf[0])
        pstr = buf[1:pstr_len+1]
        if pstr != peer.Peer.PROTOCOL:
            n.close()
            return
        hash = buf[9+pstr_len:29+pstr_len]
        if hash != self.hash:
            n.close()
            return
        n.id = buf[29+pstr_len:49+pstr_len]
        n.handshaked = True

    def handle_message_choke(self, n):
        n.p_choke = node.Node.TRUE

    def handle_message_unchoke(self, n):
        n.p_choke = node.Node.FALSE

    def handle_message_interested(self, n):
        n.p_interested = True

    def handle_message_notinterested(self, n):
        n.p_interested = False

    def handle_message_have(self, n, buf):
        index = convert.uint_ord(buf[0:4])
        n.set_piece(index)
        self.download_chunks()

    def handle_message_bitfield(self, n, buf):
        n.bitfield = []
        for byte in buf:
            mask = 0x80
            byte = ord(byte)
            for x in xrange(8):
                bit = bool(byte & mask)
                mask >>= 1
                n.bitfield.append(bit)
        self.download_chunks()

    def handle_message_piece(self, n, buf):
        index = convert.uint_ord(buf[0:4])
        begin = convert.uint_ord(buf[4:8])
        chunk = int(begin / piece.Piece.CHUNK)
        data = buf[8:]
        self.downloader.finish(n, index, chunk, data)
        self.download_chunks()

    def on_piece(self, n, index, data):
        self.writer.write(index * len(data), data)

    def on_cancel(self, n, index, chunk):
        if n in self.peer.nodes:
            self.send_message_cancel(n, index, chunk * piece.Piece.CHUNK, piece.Piece.CHUNK)
        self.download_chunks()

    def send_message_handshake(self, n):
        buf = "".join((
            chr(len(peer.Peer.PROTOCOL)),
            peer.Peer.PROTOCOL,
            convert.uint_chr(0, 8),
            self.hash,
            Torrent.id
        ))
        n.send(buf)

    def send_message(self, n, message):
        buf = "".join((
            convert.uint_chr(len(message)),
            message
        ))
        n.send(buf)

    def send_message_choke(self, n):
        self.send_message(n, chr(Torrent.MESSAGE_CHOKE))
        n.c_choke = True

    def send_message_unchoke(self, n):
        self.send_message(n, chr(Torrent.MESSAGE_UNCHOKE))
        n.c_choke = False

    def send_message_interested(self, n):
        self.send_message(n, chr(Torrent.MESSAGE_INTERESTED))
        n.c_interested = True

    def send_message_notinterested(self, n):
        self.send_message(n, chr(Torrent.MESSAGE_NOTINTERESTED))
        n.c_interested = False

    def send_message_have(self, n, index):
        buf = "".join((
            chr(Torrent.MESSAGE_HAVE),
            convert.uint_chr(index)
        ))
        self.send_message(n, buf)

    def send_message_bitfield(self, n):
        count = int(math.ceil(len(self.pieces) / 8))
        buf = [chr(Torrent.MESSAGE_BITFIELD)]
        for _ in xrange(count):
            buf.append(chr(0))
        self.send_message(n, "".join(buf))

    def send_message_request(self, n, index, begin, length):
        buf = "".join((
            chr(Torrent.MESSAGE_REQUEST),
            convert.uint_chr(index),
            convert.uint_chr(begin),
            convert.uint_chr(length)
        ))
        self.send_message(n, buf)

    def send_message_cancel(self, n, index, begin, length):
        buf = "".join((
            chr(Torrent.MESSAGE_CANCEL),
            convert.uint_chr(index),
            convert.uint_chr(begin),
            convert.uint_chr(length)
        ))
        self.send_message(n, buf)

    def _to_string(self):
        requested_nodes, all_nodes = self.downloader.nodes_count()
        string = "[%s] [%.1f%%] [%d KB / %d KB] [Peers: %d / %d]" % (
            self.torrent_path.split(os.sep)[-1],
            self.downloader.progress() * 100.0,
            self.downloader.downloaded() / 1024.0,
            self.downloader.total() / 1024.0,
            requested_nodes,
            all_nodes
        )
        return string
