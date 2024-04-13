import binascii
import struct
import hashlib
import os
import asyncio
import base64
import json
import random
import time
import urllib.parse as urlparse
from decimal import Decimal
from binascii import unhexlify, hexlify
import zstandard as zstd
from httpx import AsyncClient, Limits, Timeout
from logger_config import setup_logger
logger = setup_logger()

max_storage_tasks_in_parallel = 20
max_retrieval_tasks_in_parallel = 20
max_concurrent_requests = 5
storage_task_semaphore = asyncio.BoundedSemaphore(max_storage_tasks_in_parallel)
retrieval_task_semaphore = asyncio.BoundedSemaphore(max_retrieval_tasks_in_parallel)  # Adjust the number as needed
transaction_semaphore = asyncio.BoundedSemaphore(max_concurrent_requests)
use_parallel = 0
fee_per_kb = Decimal(0.0001)
locked_utxos = set()

def unhexstr(str):
    return binascii.unhexlify(str.encode('utf8'))

class OpCodes:
    OP_0 = 0x00
    OP_PUSHDATA1 = 0x4c
    OP_PUSHDATA2 = 0x4d
    OP_PUSHDATA4 = 0x4e
    OP_1NEGATE = 0x4f
    OP_RESERVED = 0x50
    OP_1 = 0x51
    OP_DUP = 0x76
    OP_HASH160 = 0xa9
    OP_EQUAL = 0x87
    OP_EQUALVERIFY = 0x88
    OP_CHECKSIG = 0xac
    OP_CHECKMULTISIG = 0xae
    OP_RETURN = 0x6a
    
opcodes = OpCodes()
    
class JSONRPCException(Exception):
    def __init__(self, rpc_error):
        parent_args = []
        try:
            parent_args.append(rpc_error['message'])
        except Exception as e:
            logger.info(f"Error occurred in JSONRPCException: {e}")
            pass
        Exception.__init__(self, *parent_args)
        self.error = rpc_error
        self.code = rpc_error['code'] if 'code' in rpc_error else None
        self.message = rpc_error['message'] if 'message' in rpc_error else None

    def __str__(self):
        return '%d: %s' % (self.code, self.message)

    def __repr__(self):
        return '<%s \'%s\'>' % (self.__class__.__name__, self)

def EncodeDecimal(o):
    if isinstance(o, Decimal):
        return float(round(o, 8))
    raise TypeError(repr(o) + " is not JSON serializable")

class AsyncAuthServiceProxy:
    max_concurrent_requests = 1000
    _semaphore = asyncio.BoundedSemaphore(max_concurrent_requests)
    def __init__(self, service_url, service_name=None, reconnect_timeout=25, max_retries=3, request_timeout=120, fallback_url=None):
        self.service_url = service_url
        self.service_name = service_name
        self.url = urlparse.urlparse(service_url)        
        self.client = AsyncClient(timeout=Timeout(request_timeout), limits=Limits(max_connections=max_concurrent_requests, max_keepalive_connections=100))
        self.id_count = 0
        user = self.url.username
        password = self.url.password
        authpair = f"{user}:{password}".encode('utf-8')
        self.auth_header = b'Basic ' + base64.b64encode(authpair)
        self.reconnect_timeout = reconnect_timeout
        self.max_retries = max_retries
        self.request_timeout = request_timeout
        self.circuit_breaker_open = False
        self.circuit_breaker_timeout = 60
        self.circuit_breaker_failure_threshold = 5
        self.circuit_breaker_failure_count = 0
        self.fallback_url = fallback_url
        self.max_backoff_time = 120
        self.health_check_endpoint = "/health"
        self.health_check_interval = 60
        self.use_health_check = 0

    def __getattr__(self, name):
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError
        if self.service_name is not None:
            name = f"{self.service_name}.{name}"
        return AsyncAuthServiceProxy(self.service_url, name)

    async def __call__(self, *args):
        async with self._semaphore:  # Acquire a semaphore
            if self.circuit_breaker_open:
                if self.circuit_breaker_timeout > 0:
                    logger.warning("Circuit breaker is open. Waiting for timeout...")
                    await asyncio.sleep(self.circuit_breaker_timeout)
                    self.circuit_breaker_timeout = 0
                else:
                    logger.info("Testing circuit breaker with a request...")
                    self.circuit_breaker_failure_count = 0
            self.id_count += 1
            postdata = json.dumps({
                'version': '1.1',
                'method': self.service_name,
                'params': args,
                'id': self.id_count
            }, default=EncodeDecimal)
            headers = {
                'Host': self.url.hostname,
                'User-Agent': "AuthServiceProxy/0.1",
                'Authorization': self.auth_header,
                'Content-type': 'application/json'
            }
            start_time = time.time()
            for i in range(self.max_retries):
                try:
                    if i > 0:
                        logger.warning(f"Retry attempt #{i+1}")
                        sleep_time = min(self.reconnect_timeout * (2 ** i) + random.uniform(0, self.reconnect_timeout), self.max_backoff_time)
                        logger.info(f"Waiting for {sleep_time:.2f} seconds before retrying.")
                        await asyncio.sleep(sleep_time)
                    if self.use_health_check:
                        await self.health_check()
                    response = await self.client.post(
                        self.service_url, headers=headers, data=postdata)
                    self.circuit_breaker_failure_count = 0
                    self.circuit_breaker_open = False
                    elapsed_time = time.time() - start_time
                    self.adapt_circuit_breaker_timeout(elapsed_time)
                    break
                except Exception as e:
                    logger.error(f"Error occurred in __call__: {e}")
                    logger.exception("Full stack trace:")
                    self.circuit_breaker_failure_count += 1
                    if self.circuit_breaker_failure_count >= self.circuit_breaker_failure_threshold:
                        logger.warning("Circuit breaker threshold reached. Opening circuit.")
                        self.circuit_breaker_open = True
                        self.circuit_breaker_timeout = 60
                        if self.fallback_url:
                            logger.info("Switching to fallback URL.")
                            self.service_url = self.fallback_url
                            self.url = urlparse.urlparse(self.service_url)
            else:
                logger.error("Max retries exceeded.")
                return
            response_json = response.json()
            if response_json['error'] is not None:
                raise JSONRPCException(response_json['error'])
            elif 'result' not in response_json:
                raise JSONRPCException({
                    'code': -343, 'message': 'missing JSON-RPC result'})
            else:
                return response_json['result']

    async def health_check(self):
        try:
            health_check_url = self.service_url + self.health_check_endpoint
            response = await self.client.get(health_check_url)
            if response.status_code != 200:
                raise Exception("Health check failed.")
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
            raise

    def adapt_circuit_breaker_timeout(self, elapsed_time):
        if elapsed_time > self.circuit_breaker_timeout:
            self.circuit_breaker_timeout = min(self.circuit_breaker_timeout * 1.5, 300)
        elif elapsed_time < self.circuit_breaker_timeout / 2:
            self.circuit_breaker_timeout = max(self.circuit_breaker_timeout * 0.8, 60)

def get_sha256_hash(input_data):
    if isinstance(input_data, str):
        input_data = input_data.encode('utf-8')
    return hashlib.sha3_256(input_data).hexdigest()
    
def get_raw_sha256_hash(input_data):
    if isinstance(input_data, str):
        input_data = input_data.encode('utf-8')
    return hashlib.sha3_256(input_data).digest()
        
def compress_data(input_data):
    if isinstance(input_data, str):
        input_data = input_data.encode('utf-8')
    zstd_compression_level = 22
    zstandard_compressor = zstd.ZstdCompressor(level=zstd_compression_level, write_content_size=True, write_checksum=True)
    zstd_compressed_data = zstandard_compressor.compress(input_data)
    return zstd_compressed_data

def decompress_data(compressed_data):
    return zstd.decompress(compressed_data)

async def get_unspent_transactions():
    global rpc_connection
    unspent_transactions = await rpc_connection.listunspent()
    return unspent_transactions

async def select_txins(value, burn_address="44oUgmZSL997veFEQDq569wv5tsT6KXf9QY7", number_of_utxos_to_review=100):
    global rpc_connection
    unspent = await get_unspent_transactions()
    valid_unspent = []
    reviewed_utxos = 0
    for tx in unspent:
        if not tx['spendable'] or tx['address'] == burn_address or tx['generated']:
            continue  # Skip UTXOs that are not spendable or belong to the burn address or which are coinbase transactions
        # Check if the wallet has the private key for the UTXO's address
        address_info = await rpc_connection.validateaddress(tx['address'])
        if not address_info['ismine']:
            continue  # Skip this UTXO if the wallet doesn't have the private key
        valid_unspent.append(tx)
        reviewed_utxos += 1
        if reviewed_utxos >= number_of_utxos_to_review:
            break  # Stop reviewing UTXOs if the limit is reached
    # Sort the valid UTXOs by the amount in descending order to prioritize larger amounts
    valid_unspent.sort(key=lambda x: x['confirmations'])
    selected_txins = []
    total_amount = 0
    for tx in valid_unspent:
        selected_txins.append(tx)
        total_amount += tx['amount']
        if total_amount >= value:
            break
    if total_amount < value:
        raise Exception("Insufficient funds")
    else:
        return selected_txins, total_amount
    
async def prepare_txins_and_change(value):
    global rpc_connection
    txins, total_amount = await select_txins(value)
    if txins is None or total_amount is None:
        logger.error("Insufficient funds to store the data")
        return None, None
    change = Decimal(total_amount) - Decimal(value)
    return txins, Decimal(change)

def pushdata(data):
    if len(data) < 76:
        return bytes([len(data)]) + data
    elif len(data) < 256:
        return bytes([76]) + bytes([len(data)]) + data
    elif len(data) < 65536:
        return bytes([77]) + struct.pack('<H', len(data)) + data
    else:
        return bytes([78]) + struct.pack('<I', len(data)) + data

def build_p2pkh_output_script(pubkey_hash):
    script = bytes([opcodes.OP_DUP, opcodes.OP_HASH160, len(pubkey_hash)])
    script += pubkey_hash
    script += bytes([opcodes.OP_EQUALVERIFY, opcodes.OP_CHECKSIG])
    return script

def varint(n):
    if n < 0xfd:
        return bytes([n])
    elif n < 0xffff:
        return b'\xfd' + struct.pack('<H', n)
    else:
        assert False

async def add_output_transactions(change, txouts):
    global rpc_connection
    change_address = await rpc_connection.getnewaddress()
    pubkey_hash = get_raw_sha256_hash(change_address.encode())
    script_pubkey = build_p2pkh_output_script(pubkey_hash)
    txouts.append((change, script_pubkey))
    return change

def packtxin(prevout, scriptSig, seq):
    return prevout[0][::-1] + struct.pack('<L', prevout[1]) + varint(len(scriptSig)) + scriptSig + struct.pack('<L', seq)

def packtxout(value, scriptPubKey):
    coin = 100000
    return struct.pack('<q', int(value * coin)) + varint(len(scriptPubKey)) + scriptPubKey

def packtx(txins, txouts, locktime=0, version=1):
    # We only care about transparent utxos, so we can ignore all other fields related to shielded transactions
    r = struct.pack('<I', version)  # Transaction version (4 bytes)
    r += varint(len(txins))  # Number of inputs (varint)
    for txin in txins:
        r += packtxin((unhexlify(txin['txid'])[::-1], txin['vout']), b'', 0xffffffff)
    r += varint(len(txouts))  # Number of outputs (varint)
    for (value, scriptPubKey) in txouts:
        r += packtxout(value, scriptPubKey)
    r += struct.pack('<I', locktime)  # Lock time (4 bytes)
    return r

async def send_transaction(signed_tx):
    global rpc_connection
    try:
        txid = await rpc_connection.sendrawtransaction(signed_tx)
        return txid
    except Exception as e:
        logger.error(f"Error occurred while sending transaction: {e}")
        raise

def calculate_transaction_fee(signed_tx):
    tx_size = len(signed_tx['hex']) / 2  # Convert bytes to virtual size
    fee = Decimal(tx_size) * fee_per_kb / 1000  # Calculate fee based on virtual size
    return fee

async def create_transaction(txins, txouts):
    tx = packtx(txins, txouts)
    hex_transaction = hexlify(tx).decode('utf-8')
    return hex_transaction

async def create_and_send_transaction(txins, txouts, use_parallel=True):
    global rpc_connection
    hex_transaction = await create_transaction(txins, txouts)
    signed_tx = await rpc_connection.signrawtransaction(hex_transaction)
    if signed_tx['errors']:
        logger.error(f"Error occurred while signing transaction: {signed_tx['errors']}")
        return None
    if not signed_tx['complete']:
        logger.error("Failed to sign all transaction inputs")
        return None
    fee = calculate_transaction_fee(signed_tx)
    txouts[-1] = (txouts[-1][0] - fee, txouts[-1][1])  # Subtract fee from the change output
    hex_transaction = await create_transaction(txins, txouts)
    signed_tx = await rpc_connection.signrawtransaction(hex_transaction)
    assert signed_tx['complete']
    hex_signed_transaction = signed_tx['hex']
    try:
        if use_parallel:
            async with transaction_semaphore:
                send_raw_transaction_result = await send_transaction(hex_signed_transaction)
        else:
            send_raw_transaction_result = await send_transaction(hex_signed_transaction)
        return send_raw_transaction_result, fee
    except JSONRPCException as e:
        if e.code == -25 or e.code == -26:  # -25 indicates missing inputs, -26 indicates insufficient funds
            logger.error(f"Error occurred while sending transaction: {e}")
            return None
        else:
            raise
    except Exception as e:
        logger.error(f"Error occurred while sending transaction: {e}")
        raise
def calculate_chunks(data, max_chunk_size):
    num_chunks = (len(data) + max_chunk_size - 1) // max_chunk_size
    chunk_size = (len(data) + num_chunks - 1) // num_chunks
    chunks = [data[i:i+chunk_size] for i in range(0, len(data), chunk_size)]
    return chunks

async def store_data_chunk(chunk):
    global rpc_connection
    async with storage_task_semaphore:
        fake_pubkey = os.urandom(33)  # Generate a random 33-byte public key
        script = bytes([opcodes.OP_1, len(fake_pubkey)]) + fake_pubkey + bytes([opcodes.OP_1, opcodes.OP_CHECKMULTISIG])
        txouts = [(0, script + pushdata(chunk))]
        estimated_fee = len(chunk) * fee_per_kb
        txins, change = await prepare_txins_and_change(Decimal(estimated_fee))
        if txins is None or change is None:
            logger.error("Insufficient funds to store the data")
            return None
        change = await add_output_transactions(change, txouts)
        result = await create_and_send_transaction(txins, txouts, use_parallel)
        if result:
            transaction_id, fee = result
            return transaction_id
        return None

async def store_data_chunks(chunks):
    chunk_storage_txids = []
    for i, chunk in enumerate(chunks):
        index = i.to_bytes(2, 'big')  # 2-byte index
        chunk_with_index = index + chunk
        txid = await store_data_chunk(chunk_with_index)
        chunk_storage_txids.append(txid)
    return chunk_storage_txids

async def store_chunk_txids(chunk_txids):
    global rpc_connection
    async with storage_task_semaphore:
        fake_pubkey = os.urandom(33)  # Generate a random 33-byte public key
        script = bytes([opcodes.OP_1, len(fake_pubkey)]) + fake_pubkey + bytes([opcodes.OP_1, opcodes.OP_CHECKMULTISIG])
        txids_data = b''.join(txid.encode() for txid in chunk_txids)
        txouts = [(0, script + pushdata(txids_data))]
        estimated_fee = len(txids_data) * fee_per_kb
        txins, change = await prepare_txins_and_change(Decimal(estimated_fee))
        if txins is None or change is None:
            logger.error("Insufficient funds to store the chunk TXIDs")
            return None
        change = await add_output_transactions(change, txouts)
        result = await create_and_send_transaction(txins, txouts, use_parallel)
        if result:
            transaction_id, fee = result
            return transaction_id
        return None

async def store_data_in_blockchain(input_data):
    global rpc_connection
    try:    
        await rpc_connection.lockunspent(True) # Unlock all previously locked UTXOs before starting a new transaction
        compressed_data = compress_data(input_data)
        uncompressed_data_hash = get_raw_sha256_hash(input_data)
        compressed_data_hash = get_raw_sha256_hash(compressed_data)
        uncompressed_data_length = len(input_data)
        max_chunk_size = 3000  # Maximum possible is around 9000 bytes
        header = uncompressed_data_length.to_bytes(2, 'big') + uncompressed_data_hash + compressed_data_hash
        data_with_header = header + compressed_data
        chunks = calculate_chunks(data_with_header, max_chunk_size)
        logger.info(f"Total size of compressed data: {len(compressed_data)} bytes; Data will be stored in {len(chunks)} chunks")
        try:
            chunk_txids = await store_data_chunks(chunks)
            if chunk_txids is None:
                logger.error("Error occurred while storing data chunks")
                return None
            else:
                logger.info(f"Data chunks stored successfully in the blockchain. Chunk TXIDs: {chunk_txids}")
                final_txid = await store_chunk_txids(chunk_txids)
                if final_txid is None:
                    return None
                else:
                    logger.info(f"Data stored successfully in the blockchain. Final TXID containing all chunk txids: {final_txid}")
                    return final_txid
        except Exception as e:
            logger.error(f"Error occurred while storing data in the blockchain: {e}")
            raise        
    except Exception as e:
        logger.error(f"Error occurred while storing data in the blockchain: {e}")
        raise
        
async def retrieve_data_from_blockchain(txid):
    global rpc_connection
    raw_transaction = await rpc_connection.getrawtransaction(txid)
    decoded_transaction = await rpc_connection.decoderawtransaction(raw_transaction)
    for output in decoded_transaction['vout']:
        script = unhexstr(output['scriptPubKey']['hex'])
        if script[0] == opcodes.OP_1 and script[-1] == opcodes.OP_CHECKMULTISIG:
            data = script[35:]  # Skip the fake multisig script
            if len(data) > 64:  # Check if the data is a chunk or chunk TXIDs
                try: # Try to decode the data as chunk TXIDs
                    chunk_txids = [data[i:i+64].decode() for i in range(0, len(data), 64)]
                    data_chunks = []
                    for txid in chunk_txids:
                        chunk = await retrieve_chunk(txid)
                        data_chunks.append(chunk)
                    sorted_chunks = sorted(data_chunks, key=lambda x: int.from_bytes(x[:2], 'big'))
                    combined_data = b''.join(chunk[2:] for chunk in sorted_chunks)
                except Exception as e:  # If decoding as chunk TXIDs fails, treat the data as a single chunk  # noqa: F841
                    combined_data = data
            else: # If the data is smaller than a TXID, treat it as a single chunk
                combined_data = data
            uncompressed_data_hash = combined_data[2:34]
            compressed_data_hash = combined_data[34:66]
            compressed_data = combined_data[66:]
            if get_raw_sha256_hash(compressed_data) != compressed_data_hash:
                logger.error("Compressed data hash verification failed")
                return None
            decompressed_data = decompress_data(compressed_data)
            if get_raw_sha256_hash(decompressed_data) != uncompressed_data_hash:
                logger.error("Uncompressed data hash verification failed")
                return None
            logger.info(f"Data retrieved successfully from the blockchain. Length: {len(decompressed_data)} bytes")
            return decompressed_data
    return None

async def retrieve_chunk(txid):
    global rpc_connection
    raw_transaction = await rpc_connection.getrawtransaction(txid)
    decoded_transaction = await rpc_connection.decoderawtransaction(raw_transaction)
    for output in decoded_transaction['vout']:
        script = unhexstr(output['scriptPubKey']['hex'])
        if script[0] == opcodes.OP_1 and script[-1] == opcodes.OP_CHECKMULTISIG:
            data = script[35:]  # Skip the fake multisig script
            return data
    return None
            
def get_local_rpc_settings_func(directory_with_pastel_conf=os.path.expanduser("~/.pastel/")):
    with open(os.path.join(directory_with_pastel_conf, "pastel.conf"), 'r') as f:
        lines = f.readlines()
    other_flags = {}
    rpchost = '127.0.0.1'
    rpcport = '19932'
    for line in lines:
        if line.startswith('rpcport'):
            value = line.split('=')[1]
            rpcport = value.strip()
        elif line.startswith('rpcuser'):
            value = line.split('=')[1]
            rpcuser = value.strip()
        elif line.startswith('rpcpassword'):
            value = line.split('=')[1]
            rpcpassword = value.strip()
        elif line.startswith('rpchost'):
            pass
        elif line == '\n':
            pass
        else:
            current_flag = line.strip().split('=')[0].strip()
            current_value = line.strip().split('=')[1].strip()
            other_flags[current_flag] = current_value
    return rpchost, rpcport, rpcuser, rpcpassword, other_flags

rpc_host, rpc_port, rpc_user, rpc_password, other_flags = get_local_rpc_settings_func()
rpc_connection = AsyncAuthServiceProxy(f"http://{rpc_user}:{rpc_password}@{rpc_host}:{rpc_port}")
