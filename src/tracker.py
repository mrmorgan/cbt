#!/usr/bin/python

from bencode import Bencode
import collections
import hashlib
import urllib2
import subprocess

class TrackerRequest():
    def __init__(self):
        self._Meta = {}
        self._Tracker = None

    def Meta(self, Element):
        """Saves .torrent meta data"""
        if not type(Element) in (dict, collections.OrderedDict):
            return
        self._Meta = Element

    def Request(self, PeerId, Port):
        """Sends request to tracker using GET method"""
        Get = collections.OrderedDict()
        Bencoder = Bencode()
        
        Bencoder.OpenFromElement(self._Meta["info"])
        InfoBencode = Bencoder.Encode()
        Bencoder.Close()
        InfoHash = hashlib.sha1(InfoBencode).hexdigest()
        Get["info_hash"] = InfoHash
        Get["peer_id"] = PeerId
        Get["port"] = int(Port)
        Get["uploaded"] = 0
        Get["downloaded"] = 0
        Get["left"] = 0
        Get["compact"] = 1
        Get["event"] = "started"
        
        Response = self._GetResponse(Get)
        Bencoder.OpenFromString(Response)
        print Bencoder.Decode()
        
    def _GetResponse(self, RequestArray):
        def CompileUrl(Host):
            Url = Host
            Url = "%s%s" % (Url, "&" if "?" in Url else "?")
            for Key in RequestArray:
                Url = "%s%s=%s&" % (Url, Key, RequestArray[Key])
            return Url[:-1]
        
        def TryConnect(Tracker):
            Url = CompileUrl(Tracker)
            print "Try connect to %s ..." % Url
            try:
                Response = urllib2.urlopen(Url).read()
                print "Connected :)"
                return Response
            except:
                print "Fail :("
                return False
        
        if self._Tracker:
            Response = TryConnect(self._Tracker)
            if Response:
                return Response
        if "announce" in self._Meta:
            Response = TryConnect(self._Meta["announce"])
            if Response:
                return Response
        if "announce-list" in self._Meta:
            for Item in self._Meta["announce-list"]:
                Tracker = Item[0]
                Response = TryConnect(Tracker)
                if Response:
                    return Response
        
        return ""