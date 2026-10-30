"""Owner-scoped atomic data initialization, without Calendar or embedding API calls."""
import csv
import hashlib
import re
from collections import Counter
from datetime import date, time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.search_content import search_content
from app.db.ownership import OwnershipError
from app.models.accounts import User
from app.models.entities import Event
from app.models.search import EventSearchDocument

DEFAULT_CSV=Path(__file__).resolve().parents[3]/'data/original/voice_assistant_events_1000_77e927b5d2.csv'
ORIGINAL_SHA256='81ab94b48ed7a90de6a43f729a27d2721e8cc3f70ca80c82665af289a1a54c1b'


def infer_type(title: str) -> str:
    if re.match(r'^(встреча|созвон|план[её]рка)\b',title,re.IGNORECASE):
        return 'meeting'
    if re.match(r'^напомнить\b',title,re.IGNORECASE):
        return 'reminder'
    return 'task'


def read_rows(path: Path=DEFAULT_CSV) -> list[dict]:
    rows,seen=[],set()
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        if reader.fieldnames!=['id','date','time','text']:
            raise ValueError('Expected CSV columns id,date,time,text')
        for line,row in enumerate(reader,start=2):
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('Malformed CSV row')
                source_id,title=row['id'].strip(),row['text'].strip()
                if not source_id or source_id in seen or not title:
                    raise ValueError('Missing/duplicate id or empty title')
                event_date,event_time=date.fromisoformat(row['date']),time.fromisoformat(row['time'])
                if event_time.tzinfo:
                    raise ValueError('CSV time must be local without timezone')
                seen.add(source_id)
                rows.append({'id':uuid4(),'type':infer_type(title),'title':title,'event_date':event_date,
                             'event_time':event_time,'source':'dataset','source_id':source_id,
                             'type_inferred':True,'raw_user_text':row['text'],'status':'pending'})
            except (ValueError,TypeError) as exc:
                raise ValueError(f'CSV line {line}: {exc}') from exc
    if not rows:
        raise ValueError('CSV has no records')
    return rows


def _insert_rows(session,rows):
    owner=session.info.get('owner_id')
    if owner is None or session.info.get('system_access'):
        raise OwnershipError('An explicit import owner is required')
    inserted=0
    for offset in range(0,len(rows),250):
        chunk=[row|{'owner_id':owner} for row in rows[offset:offset+250]]
        new_ids=set(session.scalars(insert(Event).values(chunk).on_conflict_do_nothing(
            constraint='uq_events_source_id').returning(Event.id)))
        documents=[]
        for row in chunk:
            if row['id'] not in new_ids:
                continue
            content=search_content(row['title'])
            documents.append({'owner_id':owner,'event_id':row['id'],'source_title':row['title'],
                              'content_hash':content.content_hash,'text_version':content.text_version})
        if documents:
            session.execute(insert(EventSearchDocument).values(documents))
        inserted+=len(new_ids)
    # Ready vectors are reused by content hash. Missing cache stays lexical-only:
    # current projections prevent changes-only worker from bulk-queuing seed texts.
    return {'rows':len(rows),'inserted':inserted,'skipped':len(rows)-inserted,
            'inferred_types':dict(Counter(row['type'] for row in rows))}


def import_dataset(session,path: Path=DEFAULT_CSV) -> dict:
    return _insert_rows(session,read_rows(path))


def ensure_original_dataset(session) -> dict:
    owner=session.info.get('owner_id')
    if owner is None or session.info.get('system_access'):
        raise OwnershipError('An explicit seed owner is required')
    if session.scalar(select(User.id).where(User.id==owner).with_for_update()) is None:
        raise OwnershipError('Seed owner does not exist')
    if hashlib.sha256(DEFAULT_CSV.read_bytes()).hexdigest()!=ORIGINAL_SHA256:
        raise ValueError('Original dataset integrity mismatch')
    rows=read_rows()
    if len(rows)!=1000:
        raise ValueError('Original dataset must contain 1000 rows')
    current=set(session.scalars(select(Event.source_id).where(Event.source=='dataset')))
    if current:
        expected={row['source_id'] for row in rows}
        # All original IDs, including soft-deleted rows, are the durable marker.
        # Never guess whether absent rows in a partial prior import were deleted.
        state='initialized' if expected<=current else 'existing_import_requires_review'
        return {'state':state,'rows':1000,'inserted':0,'skipped':1000,'existing_source_rows':len(current)}
    return {'state':'initialized',**_insert_rows(session,rows)}
