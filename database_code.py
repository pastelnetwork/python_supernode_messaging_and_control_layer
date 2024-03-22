from logger_config import setup_logger
from datetime import datetime
from decimal import Decimal
import traceback
from sqlalchemy import Column, String, DateTime, Integer, Numeric, LargeBinary, Text, text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from decouple import Config as DecoupleConfig, RepositoryEnv
from pydantic import BaseModel
from typing import Optional

class MessageModel(BaseModel):
    message: str
    message_type: str

class SendMessageResponse(BaseModel):
    status: str
    message: str

class SupernodeData(BaseModel):
    supernode_status: str
    protocol_version: str
    supernode_psl_address: str
    lastseentime: datetime
    activeseconds: int
    lastpaidtime: datetime
    lastpaidblock: int
    ipaddress_port: str
    rank: int
    pubkey: str
    extAddress: Optional[str]
    extP2P: Optional[str]
    extKey: Optional[str]
    activedays: float

    class Config:
        from_attributes = True
        populate_by_name = True

class LocalMachineSupernodeInfo(BaseModel):
    local_machine_supernode_data: SupernodeData
    local_sn_rank: int
    local_sn_pastelid: str
    local_machine_ip_with_proper_port: str

    class Config:
        from_attributes = True
        
config = DecoupleConfig(RepositoryEnv('.env'))
DATABASE_URL = config.get("DATABASE_URL", cast=str, default="sqlite+aiosqlite:///super_node_messaging_and_control_layer.sqlite")
logger = setup_logger()
Base = declarative_base()

class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    sending_sn_pastelid = Column(String, index=True)
    receiving_sn_pastelid = Column(String, index=True)
    sending_sn_txid_vout = Column(String, index=True)
    receiving_sn_txid_vout = Column(String, index=True)    
    message_type = Column(String, index=True)
    message_body = Column(Text)
    signature = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<Message(id={self.id}, sending_sn_pastelid='{self.sending_sn_pastelid}', receiving_sn_pastelid='{self.receiving_sn_pastelid}', message_type='{self.message_type}', timestamp='{self.timestamp}')>"

class MessageMetadata(Base):
    __tablename__ = "message_metadata"

    id = Column(Integer, primary_key=True, index=True)
    total_messages = Column(Integer)
    total_senders = Column(Integer)
    total_receivers = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    
class MessageSenderMetadata(Base):
    __tablename__ = "message_sender_metadata"

    id = Column(Integer, primary_key=True, index=True)
    sending_sn_pastelid = Column(String, index=True)
    sending_sn_txid_vout = Column(String, index=True)
    sending_sn_pubkey = Column(String, index=True)    
    total_messages_sent = Column(Integer)
    total_data_sent_bytes = Column(Numeric(precision=20, scale=2))
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

class MessageReceiverMetadata(Base):
    __tablename__ = "message_receiver_metadata"

    id = Column(Integer, primary_key=True, index=True)
    receiving_sn_pastelid = Column(String, index=True)
    receiving_sn_txid_vout = Column(String, index=True)
    total_messages_received = Column(Integer)
    total_data_received_bytes = Column(Numeric(precision=20, scale=2))
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

class MessageSenderReceiverMetadata(Base):
    __tablename__ = "message_sender_receiver_metadata"

    id = Column(Integer, primary_key=True, index=True)
    sending_sn_pastelid = Column(String, index=True)
    receiving_sn_pastelid = Column(String, index=True)
    total_messages = Column(Integer)
    total_data_bytes = Column(Numeric(precision=20, scale=2))
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
        
def to_serializable(val):
    if isinstance(val, datetime):
        return val.isoformat()
    elif isinstance(val, Decimal):
        return float(val)
    else:
        return str(val)

def to_dict(self):
    d = {}
    for column in self.__table__.columns:
        if not isinstance(column.type, LargeBinary):
            value = getattr(self, column.name)
            if value is not None:
                serialized_value = to_serializable(value)
                d[column.name] = serialized_value if serialized_value is not None else value
    return d

Message.to_dict = to_dict

async def get_db():
    db = AsyncSessionLocal()
    try:
        yield db
        await db.commit()
    except Exception as e:
        tb_str = traceback.format_exception(etype=type(e), value=e, tb=e.__traceback__)
        tb_str = "".join(tb_str)        
        logger.error(f"Database Error: {e}\nFull Traceback:\n{tb_str}")
        await db.rollback()
        raise
    finally:
        await db.close()

engine = create_async_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})
    
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

async def initialize_db():
    list_of_sqlite_pragma_strings = [
        "PRAGMA journal_mode=WAL;", 
        "PRAGMA synchronous = NORMAL;", 
        "PRAGMA cache_size = -262144;", 
        "PRAGMA busy_timeout = 2000;", 
        "PRAGMA wal_autocheckpoint = 100;"
    ]
    list_of_sqlite_pragma_justification_strings = [
        "Set SQLite to use Write-Ahead Logging (WAL) mode (from default DELETE mode) so that reads and writes can occur simultaneously",
        "Set synchronous mode to NORMAL (from FULL) so that writes are not blocked by reads",
        "Set cache size to 1GB (from default 2MB) so that more data can be cached in memory and not read from disk; to make this 256MB, set it to -262144 instead",
        "Increase the busy timeout to 2 seconds so that the database waits",
        "Set the WAL autocheckpoint to 100 (from default 1000) so that the WAL file is checkpointed more frequently"
    ]
    assert(len(list_of_sqlite_pragma_strings) == len(list_of_sqlite_pragma_justification_strings))

    try:
        async with engine.begin() as conn:
            for pragma_string in list_of_sqlite_pragma_strings:
                await conn.execute(sql_text(pragma_string))
            await conn.run_sync(Base.metadata.create_all)  # Create tables if they don't exist
        await engine.dispose()
        return True
    except Exception as e:
        logger.error(f"Database Initialization Error: {e}")
        return False
    