from collections import defaultdict
import hashlib
import json
import logging
from pprint import pformat
import random
from threading import Thread
import traceback
from urlparse import urlparse
import xmlrpclib

import gnupg
import obelisk
from pybitcointools.main import privkey_to_pubkey, privtopub, random_key
from pysqlcipher.dbapi2 import OperationalError, DatabaseError
import zmq
from zmq.eventloop import ioloop
from zmq.eventloop.ioloop import PeriodicCallback

import connection
from crypto_util import Cryptor
from dht import DHT
import network_util
from protocol import hello_request, hello_response, goodbye, proto_response_pubkey


class TransportLayer(object):
    # Transport layer manages a list of peers
    def __init__(self, ob_ctx, guid, nickname=None):
        self.peers = {}
        self.callbacks = defaultdict(list)
        self.timeouts = []
        self.port = ob_ctx.server_port
        self.ip = ob_ctx.server_ip
        self.guid = guid
        self.market_id = ob_ctx.market_id
        self.nickname = nickname
        self.handler = None
        self.uri = network_util.get_peer_url(self.ip, self.port)
        self.listener = None

        # Create one ZeroMQ context to be reused and reduce overhead
        self.ctx = zmq.Context.instance()

        self.log = logging.getLogger(
            '[%s] %s' % (ob_ctx.market_id, self.__class__.__name__)
        )

    def start_listener(self):
        self.listener = connection.PeerListener(self.ip, self.port, self.ctx, self._on_raw_message)
        self.listener.listen()

    def add_callbacks(self, callbacks):
        for section, callback in callbacks:
            self.callbacks[section] = []
            self.add_callback(section, callback)

    def set_websocket_handler(self, handler):
        self.handler = handler

    def add_callback(self, section, callback):
        if callback not in self.callbacks[section]:
            self.callbacks[section].append(callback)

    def trigger_callbacks(self, section, *data):

        # Run all callbacks in specified section
        for cb in self.callbacks[section]:
            if cb['validator_cb'](*data):
                cb['cb'](*data)

        # Run all callbacks registered under the 'all' section. Don't duplicate
        # calls if the specified section was 'all'.
        if not section == 'all':
            for cb in self.callbacks['all']:
                if cb['validator_cb'](*data):
                    cb['cb'](*data)

    def get_profile(self):
        return hello_request({'uri': self.uri})

    def _init_peer(self, msg):
        uri = msg['uri']

        if uri not in self.peers:
            self.peers[uri] = connection.PeerConnection(self, uri)

    def send(self, data, send_to=None, callback=lambda msg: None):

        self.log.info("Outgoing Data: %s %s", data, send_to)
        data['senderNick'] = self.nickname

        # Directed message
        if send_to is not None:
            peer = self.dht.routingTable.getContact(send_to)
            peer.send(data, callback=callback)
            return

        else:
            # FindKey and then send

            for peer in self.dht.activePeers:
                try:
                    data['senderGUID'] = self.guid
                    data['pubkey'] = self.pubkey

                    def cb(msg):
                        print msg
                    peer.send(data, cb)

                except Exception:
                    self.log.info("Error sending over peer!")
                    traceback.print_exc()

    def store(self, *args, **kwargs):
        """
        Store or republish data.

        Refer to the dht module (iterativeStore()) for further details.
        """
        self.dht.iterativeStore(*args, **kwargs)

    def broadcast_goodbye(self):
        self.log.info("Broadcast goodbye")
        msg = goodbye({'uri': self.uri})
        self.send(msg)

    def _on_message(self, msg):

        # here goes the application callbacks
        # we get a "clean" msg which is a dict holding whatever
        self.log.info("[On Message] Data received: %s", msg)

        if msg['type'] != 'ok':
            self.trigger_callbacks(msg['type'], msg)

    def _on_raw_message(self, msg):
        msg_type = msg.get('type')
        if msg_type == 'hello_request' and msg.get('uri'):
            self._init_peer(msg)
        else:
            self._on_message(msg)

    def valid_peer_uri(self, uri):
        try:
            [_, self_addr, _] = network_util.uri_parts(self.uri)
            [other_protocol, other_addr, other_port] = \
                network_util.uri_parts(uri)
        except RuntimeError:
            return False

        if not network_util.is_valid_protocol(other_protocol) \
                or not network_util.is_valid_port(other_port):
            return False

        if network_util.is_private_ip_address(self_addr):
            if not network_util.is_private_ip_address(other_addr):
                self.log.warning((
                    'Trying to connect to external '
                    'network with a private ip address.'
                ))
        else:
            if network_util.is_private_ip_address(other_addr):
                return False

        return True

    def shutdown(self):
        raise NotImplementedError


class CryptoTransportLayer(TransportLayer):

    def __init__(self, ob_ctx, db):

        self.ob_ctx = ob_ctx

        self.log = logging.getLogger(
            '[%s] %s' % (ob_ctx.market_id, self.__class__.__name__)
        )
        requests_log = logging.getLogger("requests")
        requests_log.setLevel(logging.WARNING)

        self.db = db

        self.bitmessage_api = None
        if (ob_ctx.bm_user, ob_ctx.bm_pass, ob_ctx.bm_port) != (None, None, -1):
            if not self._connect_to_bitmessage():
                self.log.info('Bitmessage not installed or started')

        self.market_id = ob_ctx.market_id
        self.nick_mapping = {}
        self.uri = network_util.get_peer_url(ob_ctx.server_ip, ob_ctx.server_port)
        self.ip = ob_ctx.server_ip
        self.nickname = ""
        self.dev_mode = ob_ctx.dev_mode

        self.all_messages = (
            'hello',
            'findNode',
            'findNodeResponse',
            'store'
        )

        # Set up
        self._setup_settings()

        ob_ctx.market_id = self.market_id

        self.dht = DHT(self, self.market_id, self.settings, self.db)

        TransportLayer.__init__(self, ob_ctx, self.guid, self.nickname)

        self.start_listener()

        if ob_ctx.enable_ip_checker and not ob_ctx.seed_mode and not ob_ctx.dev_mode:
            self.start_ip_address_checker()

    def start_listener(self):

        self.add_callbacks([
            (
                msg,
                {
                    'cb': getattr(self, 'on_%s' % msg),
                    'validator_cb': getattr(self, 'validate_on_%s' % msg)
                }
            )
            for msg in self.all_messages
        ])

        self.listener = connection.CryptoPeerListener(
            self.ip, self.port, self.pubkey, self.secret, self.ctx,
            self._on_message
        )

        self.listener.set_ok_msg({
            'type': 'ok',
            'senderGUID': self.guid,
            'pubkey': self.pubkey,
            'senderNick': self.nickname
        })
        self.listener.listen()

    def start_ip_address_checker(self):
        '''Checks for possible public IP change'''
        if self.ob_ctx.enable_ip_checker:
            self.caller = PeriodicCallback(self._ip_updater_periodic_callback, 5000, ioloop.IOLoop.instance())
            self.caller.start()
            self.log.info("IP_CHECKER_ENABLED: Periodic IP Address Checker started.")

    def _ip_updater_periodic_callback(self):
        if self.ob_ctx.enable_ip_checker:
            new_ip = network_util.get_my_ip()

            if not new_ip or new_ip == self.ip:
                return

            self.ob_ctx.server_ip = new_ip
            self.ip = new_ip

            if self.listener is not None:
                self.listener.set_ip_address(new_ip)

            self.dht._iterativeFind(self.guid, [], 'findNode')

    def save_peer_to_db(self, peer_tuple):
        uri = peer_tuple[0]
        pubkey = peer_tuple[1]
        guid = peer_tuple[2]
        nickname = peer_tuple[3]

        # Update query
        self.db.deleteEntries("peers", {"uri": uri, "guid": guid}, "OR")
        if guid is not None:
            self.db.insertEntry("peers", {
                "uri": uri,
                "pubkey": pubkey,
                "guid": guid,
                "nickname": nickname,
                "market_id": self.market_id
            })

    def _connect_to_bitmessage(self):
        # Get bitmessage going
        # First, try to find a local instance
        result = False
        bm_user = self.ob_ctx.bm_user
        bm_pass = self.ob_ctx.bm_pass
        bm_port = self.ob_ctx.bm_port
        try:
            self.log.info(
                '[_connect_to_bitmessage] Connecting to Bitmessage on port %s',
                bm_port
            )
            self.bitmessage_api = xmlrpclib.ServerProxy(
                "http://{}:{}@localhost:{}/".format(bm_user, bm_pass, bm_port),
                verbose=0
            )
            result = self.bitmessage_api.add(2, 3)
            self.log.info(
                "[_connect_to_bitmessage] Bitmessage API is live: %s",
                result
            )
        # If we failed, fall back to starting our own
        except Exception as e:
            self.log.info("Failed to connect to bitmessage instance: %s", e)
            self.bitmessage_api = None
        return result

    def get_dht(self):
        return self.dht

    def get_market_id(self):
        return self.market_id

    def validate_on_hello(self, msg):
        self.log.debug('Validating ping message.')
        return True

    def on_hello(self, msg):
        self.log.info('Pinged %s', json.dumps(msg, ensure_ascii=False))

    def validate_on_store(self, msg):
        self.log.debug('Validating store value message.')
        return True

    def on_store(self, msg):
        self.dht._on_storeValue(msg)

    def validate_on_findNode(self, msg):
        self.log.debug('Validating find node message.')
        return True

    def on_findNode(self, msg):
        self.dht.on_find_node(msg)

    def validate_on_findNodeResponse(self, msg):
        self.log.debug('Validating find node response message.')
        return True

    def on_findNodeResponse(self, msg):
        self.dht.on_findNodeResponse(self, msg)

    def _setup_settings(self):
        try:
            self.settings = self.db.selectEntries("settings", {"market_id": self.market_id})
        except (OperationalError, DatabaseError) as e:
            print e
            raise SystemExit("database file %s corrupt or empty - cannot continue" % self.db.db_path)

        if len(self.settings) == 0:
            self.settings = {"market_id": self.market_id, "welcome": "enable"}
            self.db.insertEntry("settings", self.settings)
        else:
            self.settings = self.settings[0]

        # Generate PGP key during initial setup or if previous PGP gen failed
        if not ('PGPPubKey' in self.settings and self.settings["PGPPubKey"]):
            try:
                self.log.info('Generating PGP keypair. This may take several minutes...')
                print 'Generating PGP keypair. This may take several minutes...'
                gpg = gnupg.GPG()
                input_data = gpg.gen_key_input(key_type="RSA",
                                               key_length=2048,
                                               name_email='gfy@gfy.com',
                                               name_comment="Autogenerated by Open Bazaar",
                                               passphrase="P@ssw0rd")
                assert input_data is not None
                key = gpg.gen_key(input_data)
                assert key is not None

                pubkey_text = gpg.export_keys(key.fingerprint)
                newsettings = {"PGPPubKey": pubkey_text, "PGPPubkeyFingerprint": key.fingerprint}
                self.db.updateEntries("settings", newsettings, {"market_id": self.market_id})
                self.settings.update(newsettings)

                self.log.info('PGP keypair generated.')
            except Exception as e:
                self.log.error("Encountered a problem with GPG: %s", e)
                raise SystemExit("Encountered a problem with GPG: %s" % e)

        if not ('pubkey' in self.settings and self.settings['pubkey']):
            # Generate Bitcoin keypair
            self._generate_new_keypair()

        if not ('nickname' in self.settings and self.settings['nickname']):
            newsettings = {'nickname': 'Default'}
            self.db.updateEntries('settings', newsettings, {"market_id": self.market_id})
            self.settings.update(newsettings)

        self.nickname = self.settings['nickname'] if 'nickname' in self.settings else ""
        self.secret = self.settings['secret'] if 'secret' in self.settings else ""
        self.pubkey = self.settings['pubkey'] if 'pubkey' in self.settings else ""
        self.privkey = self.settings.get('privkey')
        self.btc_pubkey = privkey_to_pubkey(self.privkey)
        self.guid = self.settings['guid'] if 'guid' in self.settings else ""
        self.sin = self.settings['sin'] if 'sin' in self.settings else ""
        self.bitmessage = self.settings['bitmessage'] if 'bitmessage' in self.settings else ""

        if not ('bitmessage' in self.settings and self.settings['bitmessage']):
            # Generate Bitmessage address
            if self.bitmessage_api is not None:
                self._generate_new_bitmessage_address()

        self.cryptor = Cryptor(pubkey_hex=self.pubkey, privkey_hex=self.secret)

        # In case user wants to override with command line passed bitmessage values
        if self.ob_ctx.bm_user is not None and \
           self.ob_ctx.bm_pass is not None and \
           self.ob_ctx.bm_port is not None:
            self._connect_to_bitmessage()

    def _generate_new_keypair(self):
        secret = str(random.randrange(2 ** 256))
        self.secret = hashlib.sha256(secret).hexdigest()
        self.pubkey = privtopub(self.secret)
        self.privkey = random_key()
        self.btc_pubkey = privtopub(self.privkey)
        print 'PUBLIC KEY: ', self.btc_pubkey

        # Generate SIN
        sha_hash = hashlib.sha256()
        sha_hash.update(self.pubkey)
        ripe_hash = hashlib.new('ripemd160')
        ripe_hash.update(sha_hash.digest())

        self.guid = ripe_hash.digest().encode('hex')
        self.sin = obelisk.EncodeBase58Check('\x0F\x02%s' + ripe_hash.digest())

        newsettings = {
            "secret": self.secret,
            "pubkey": self.pubkey,
            "privkey": self.privkey,
            "guid": self.guid,
            "sin": self.sin
        }
        self.db.updateEntries("settings", newsettings, {"market_id": self.market_id})
        self.settings.update(newsettings)

    def _generate_new_bitmessage_address(self):
        # Use the guid generated previously as the key
        self.bitmessage = self.bitmessage_api.createRandomAddress(
            self.guid.encode('base64'),
            False,
            1.05,
            1.1111
        )
        newsettings = {"bitmessage": self.bitmessage}
        self.db.updateEntries("settings", newsettings, {"market_id": self.market_id})
        self.settings.update(newsettings)

    def join_network(self, seeds=None, callback=lambda msg: None):
        if seeds is None:
            seeds = []

        self.log.info('Joining network')

        # Connect up through seed servers
        for idx, seed in enumerate(seeds):
            seeds[idx] = network_util.get_peer_url(seed, "12345")

        # Connect to persisted peers
        db_peers = self.get_past_peers()

        known_peers = list(set(seeds).union(db_peers))

        for known_peer in known_peers:
            self.dht.add_peer(self, known_peer)

        # Populate routing table by searching for self
        if known_peers:
            # Check every one second if we are connected
            # We could use a PeriodicCallback but I think this is simpler
            # since this will be repeated in most cases less than 10 times
            def join_callback():
                # If we are not connected to any node, reschedule a check
                if not self.dht.activePeers:
                    ioloop.IOLoop.instance().call_later(1, join_callback)
                else:
                    self.search_for_my_node()
            join_callback()

        if callback is not None:
            callback('Joined')

    def get_past_peers(self):
        peers = []
        result = self.db.selectEntries("peers", {"market_id": self.market_id})
        for peer in result:
            peers.append(peer['uri'])
        return peers

    def search_for_my_node(self):
        self.log.info('Searching for myself')
        self.dht._iterativeFind(self.guid, self.dht.knownNodes, 'findNode')

    def get_crypto_peer(self, guid=None, uri=None, pubkey=None, nickname=None):
        if guid == self.guid:
            self.log.error('Cannot get CryptoPeerConnection for your own node')
            return

        self.log.debug(
            'Getting CryptoPeerConnection'
            '\nGUID: %s'
            '\nURI: %s'
            '\nPubkey:%s'
            '\nNickname:%s',
            guid, uri, pubkey, nickname
        )

        return connection.CryptoPeerConnection(
            self, uri, pubkey, guid=guid, nickname=nickname
        )

    def get_profile(self):
        peers = {}

        self.settings = self.db.selectEntries("settings", {"market_id": self.market_id})[0]
        for uri, peer in self.peers.iteritems():
            if peer.pub:
                peers[uri] = peer.pub.encode('hex')
        return {'uri': self.uri,
                'pub': self.cryptor.get_pubkey().encode('hex'),
                'nickname': self.nickname,
                'peers': peers}

    def respond_pubkey_if_mine(self, nickname, ident_pubkey):

        if ident_pubkey != self.pubkey:
            self.log.info("Public key does not match your identity")
            return

        # Return signed pubkey
        pubkey = self.cryptor.pubkey  # XXX: Neither a Cryptor nor ECC has such a field.
        ec_key = obelisk.EllipticCurveKey()
        ec_key.set_secret(self.secret)
        digest = obelisk.Hash(pubkey)
        signature = ec_key.sign(digest)

        # Send array of nickname, pubkey, signature to transport layer
        self.send(proto_response_pubkey(nickname, pubkey, signature))

    def pubkey_exists(self, pub):

        for peer in self.peers.itervalues():
            self.log.info(
                'PEER: %s Pub: %s',
                peer.pub.encode('hex'), pub.encode('hex')
            )
            if peer.pub.encode('hex') == pub.encode('hex'):
                return True

        return False

    def create_peer(self, uri, pub, node_guid):

        if pub:
            pub = pub.decode('hex')

        # Create the peer if public key is not already in the peer list
        self.peers[uri] = connection.CryptoPeerConnection(self, uri, pub, node_guid)

        # Call 'peer' callbacks on listeners
        self.trigger_callbacks('peer', self.peers[uri])

    def send(self, data, send_to=None, callback=lambda msg: None):

        self.log.debug("Outgoing Data: %s %s", data, send_to)

        # Directed message
        if send_to is not None:

            peer = self.dht.routingTable.getContact(send_to)
            if peer is None:
                for activePeer in self.dht.activePeers:
                    if activePeer.guid == send_to:
                        peer = activePeer
                        break

            if peer:
                self.log.debug('Directed Data (%s): %s', send_to, data)
                try:
                    peer.send(data, callback=callback)
                except Exception as e:
                    self.log.error('Not sending message directly to peer %s', e)
            else:
                self.log.error('No peer found')

        else:
            # FindKey and then send

            for peer in self.dht.activePeers:
                try:
                    routing_peer = self.dht.routingTable.getContact(peer.guid)

                    if routing_peer is None:
                        self.dht.routingTable.addContact(peer)
                        routing_peer = peer

                    data['senderGUID'] = self.guid
                    data['pubkey'] = self.pubkey

                    def cb(msg):
                        self.log.debug('Message Back: \n%s', pformat(msg))

                    routing_peer.send(data, cb)

                except Exception:
                    self.log.info("Error sending over peer!")
                    traceback.print_exc()

    def send_enc(self, uri, msg):
        peer = self.peers[uri]
        pub = peer.pub

        # Now send a hello message to the peer
        if pub:
            self.log.info(
                "Sending encrypted [%s] message to %s",
                msg['type'], uri
            )
            peer.send(msg)
        else:
            # Will send clear profile on initial if no pub
            self.log.info(
                "Sending unencrypted [%s] message to %s",
                msg['type'], uri
            )
            self.peers[uri].send_raw(json.dumps(msg))

    def _init_peer(self, msg):

        uri = msg['uri']
        pub = msg.get('pub')
        nickname = msg.get('nickname')
        msg_type = msg.get('type')
        guid = msg['guid']

        if not self.valid_peer_uri(uri):
            self.log.error("Invalid Peer: %s", uri)
            return

        if uri not in self.peers:
            # Unknown peer
            self.log.info('Add New Peer: %s', uri)
            self.create_peer(uri, pub, guid)

            if not msg_type:
                self.send_enc(uri, hello_request(self.get_profile()))
            elif msg_type == 'hello_request':
                self.send_enc(uri, hello_response(self.get_profile()))

        else:
            # Known peer
            if pub:
                # test if we have to update the pubkey
                if not self.peers[uri].pub:
                    self.log.info("Setting public key for seed node")
                    self.peers[uri].pub = pub.decode('hex')
                    self.trigger_callbacks('peer', self.peers[uri])

                if self.peers[uri].pub != pub.decode('hex'):
                    self.log.info("Updating public key for node")
                    self.peers[uri].nickname = nickname
                    self.peers[uri].pub = pub.decode('hex')

                    self.trigger_callbacks('peer', self.peers[uri])

            if msg_type == 'hello_request':
                # reply only if necessary
                self.send_enc(uri, hello_response(self.get_profile()))

    def _on_message(self, msg):

        # here goes the application callbacks
        # we get a "clean" msg which is a dict holding whatever

        pubkey = msg.get('pubkey')
        uri = msg.get('uri')
        ip = urlparse(uri).hostname
        port = urlparse(uri).port
        guid = msg.get('senderGUID')
        nickname = msg.get('senderNick')[:120]

        self.dht.add_known_node((ip, port, guid, nickname))
        self.log.info('On Message: %s', json.dumps(msg, ensure_ascii=False))
        self.dht.add_peer(self, uri, pubkey, guid, nickname)
        t = Thread(target=self.trigger_callbacks, args=(msg['type'], msg,))
        t.start()

    def shutdown(self):
        print "CryptoTransportLayer.shutdown()!"
        print "Notice: explicit DHT Shutdown not implemented."

        try:
            self.bitmessage_api.close()
        except Exception as e:
            # It might not even be open; we can't do much more on our
            # way out if exception is thrown here.
            self.log.error(
                "Could not shutdown bitmessage_api's ServerProxy: %s", e.message
            )
