"""Channel-scoped persistence using the Phase 2 schemas and mapping.

Borrow a connection from connect_database(); this class never opens/closes it.
Writes are atomic and nest as savepoints inside transaction() or a caller's
transaction. No migration, production import, or network call occurs on import.
"""
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import AwareDatetime, TypeAdapter

from engine.learning.mapping import TABLES, to_columns, from_row
from engine.learning.schema import (
    Identifier, ChannelProfile, GenerationRecord, PublishedVideo,
    MetricSnapshot, ContentFeatures, LearningContext, LearningObservation,
)

_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(AwareDatetime)


class RepositoryError(RuntimeError):
    """Credential-free database error; original driver detail is suppressed."""


class RepositoryConflict(RepositoryError):
    """Duplicate identity, stale update, or changed publication identity."""


class LearningRepository:
    def __init__(self, connection, channel_context_id, *, schema="learning"):
        self._connection = connection
        self._channel = _ID.validate_python(channel_context_id)
        self._schema = _ID.validate_python(schema)

    @property
    def channel_context_id(self):
        return self._channel

    @contextmanager
    def transaction(self):
        """Group related operations; an escaping failure rolls back the group."""
        try:
            with self._connection.transaction():
                yield self
        except psycopg.errors.UniqueViolation:
            raise RepositoryConflict("Learning record identity already exists.") from None
        except psycopg.Error:
            raise RepositoryError("Learning database operation failed; transaction rolled back.") from None

    def _table(self, model):
        return sql.Identifier(self._schema, TABLES[model])

    @staticmethod
    def _columns(model):
        # Derive projection from the schema, including flattened window fields.
        result = []
        for field in model.model_fields:
            if model is LearningContext and field in {"observations", "examples"}:
                continue
            if field in {"observation_window", "analysis_period"}:
                result.extend(f"{field}_{part}" for part in ("kind", "start_at", "end_at"))
            else:
                result.append(field)
        if model is LearningContext:
            result.extend(("examples_present", "sealed"))
        return sql.SQL(", ").join(map(sql.Identifier, result))

    def _write(self, model, record, *, upsert=False, identity=None):
        if type(record) is not model:
            raise TypeError(f"Expected {model.__name__}.")
        record.require_channel(self._channel)
        columns = to_columns(record)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            self._table(model), sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        if upsert:
            keys = ["channel_context_id"] + ([identity] if identity else [])
            immutable = set(keys) | {"created_at"}
            if model is PublishedVideo:
                immutable.add("youtube_video_id")
            updates = [name for name in columns if name not in immutable]
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(name), sql.Identifier(name))
                for name in updates
            )
            table = sql.Identifier(TABLES[model])
            guard = sql.SQL("{}.created_at = EXCLUDED.created_at AND {}.updated_at <= EXCLUDED.updated_at").format(table, table)
            if model is PublishedVideo:
                guard += sql.SQL(" AND {}.youtube_video_id = EXCLUDED.youtube_video_id").format(table)
            query += sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {} WHERE {}").format(
                sql.SQL(", ").join(map(sql.Identifier, keys)), assignments, guard,
            )
        query += sql.SQL(" RETURNING {} ").format(self._columns(model))
        with self.transaction():
            with self._connection.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute(query, tuple(columns.values())).fetchone()
            if row is None:
                raise RepositoryConflict("Stale update or immutable record identity mismatch.")
            return from_row(model, row, channel_context_id=self._channel)

    def _select(self, model, *, filters=(), time_column=None, start_at=None,
                end_at=None, order=(), limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000.")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer.")
        clauses = [sql.SQL("channel_context_id = %s")]
        params = [self._channel]
        for column, value in filters:
            clauses.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
            params.append(_ID.validate_python(value))
        start = _TIME.validate_python(start_at) if start_at is not None else None
        end = _TIME.validate_python(end_at) if end_at is not None else None
        if start is not None and end is not None and start > end:
            raise ValueError("start_at must not follow end_at.")
        if start is not None:
            clauses.append(sql.SQL("{} >= %s").format(sql.Identifier(time_column)))
            params.append(start)
        if end is not None:
            clauses.append(sql.SQL("{} < %s").format(sql.Identifier(time_column)))
            params.append(end)
        query = sql.SQL("SELECT {} FROM {} WHERE {}").format(
            self._columns(model), self._table(model), sql.SQL(" AND ").join(clauses),
        )
        if order:
            query += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(
                sql.SQL("{} {} NULLS LAST").format(sql.Identifier(column), sql.SQL(direction))
                for column, direction in order
            )
        query += sql.SQL(" LIMIT %s OFFSET %s")
        params.extend((limit, offset))
        with self.transaction():
            with self._connection.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(query, tuple(params)).fetchall()
            return [self._read_context(row) if model is LearningContext else
                    from_row(model, row, channel_context_id=self._channel) for row in rows]

    def _get(self, model, column, value):
        rows = self._select(model, filters=((column, value),), limit=1)
        return rows[0] if rows else None

    def create_profile(self, record):
        return self._write(ChannelProfile, record)

    def upsert_profile(self, record):
        return self._write(ChannelProfile, record, upsert=True)

    def get_profile(self):
        return self._get(ChannelProfile, "channel_context_id", self._channel)

    def create_generation(self, record):
        return self._write(GenerationRecord, record)

    def get_generation(self, generation_id):
        return self._get(GenerationRecord, "generation_id", generation_id)

    def list_generations(self, *, start_at=None, end_at=None, limit=100, offset=0):
        """Newest first; time range is [start_at, end_at)."""
        return self._select(GenerationRecord, time_column="generated_at", start_at=start_at,
                            end_at=end_at, order=(("generated_at", "DESC"), ("generation_id", "ASC")), limit=limit, offset=offset)

    def recent_generations(self, *, limit=10):
        return self.list_generations(limit=limit)

    def create_published_video(self, record):
        return self._write(PublishedVideo, record)

    def upsert_published_video(self, record):
        return self._write(PublishedVideo, record, upsert=True, identity="published_video_id")

    def get_published_video(self, published_video_id):
        return self._get(PublishedVideo, "published_video_id", published_video_id)

    def get_published_video_by_youtube_id(self, youtube_video_id):
        return self._get(PublishedVideo, "youtube_video_id", youtube_video_id)

    def published_videos_for_generation(self, generation_id, *, limit=100, offset=0):
        return self._select(PublishedVideo, filters=(("linked_generation_id", generation_id),),
                            order=(("published_at", "DESC"), ("published_video_id", "ASC")), limit=limit, offset=offset)

    def list_published_videos(self, *, start_at=None, end_at=None, limit=100, offset=0):
        return self._select(PublishedVideo, time_column="published_at", start_at=start_at,
                            end_at=end_at, order=(("published_at", "DESC"), ("published_video_id", "ASC")), limit=limit, offset=offset)

    def recent_published_videos(self, *, limit=10):
        return self.list_published_videos(limit=limit)

    def insert_snapshot(self, record):
        return self._write(MetricSnapshot, record)

    def get_snapshot(self, snapshot_id):
        return self._get(MetricSnapshot, "snapshot_id", snapshot_id)

    def list_snapshots(self, *, youtube_video_id=None, start_at=None, end_at=None, limit=100, offset=0):
        """Oldest first; filter dates by collection time, not publication time."""
        filters = (("youtube_video_id", youtube_video_id),) if youtube_video_id is not None else ()
        return self._select(MetricSnapshot, filters=filters, time_column="collected_at", start_at=start_at,
                            end_at=end_at, order=(("collected_at", "ASC"), ("snapshot_id", "ASC")), limit=limit, offset=offset)

    def create_features(self, record):
        return self._write(ContentFeatures, record)

    def get_features(self, feature_record_id):
        return self._get(ContentFeatures, "feature_record_id", feature_record_id)

    def list_features(self, *, generation_id=None, published_video_id=None, limit=100, offset=0):
        filters = []
        if generation_id is not None:
            filters.append(("linked_generation_id", generation_id))
        if published_video_id is not None:
            filters.append(("linked_video_id", published_video_id))
        return self._select(ContentFeatures, filters=filters, order=(("created_at", "DESC"), ("feature_record_id", "ASC")), limit=limit, offset=offset)

    def validate_context_evidence(self, record):
        """Resolve typed references in this repository's channel; never guess targets."""
        if type(record) is not LearningContext:
            raise TypeError("Expected LearningContext.")
        record = LearningContext.model_validate(record.model_dump()).require_channel(self._channel)
        if not record.observations or record.sample_count < 1:
            raise ValueError("Empty context rejected.")
        methods = {"channel_profile": lambda key: self.get_profile() if key == self._channel else None,
                   "generation": self.get_generation, "published_video": self.get_published_video,
                   "metric_snapshot": self.get_snapshot, "content_features": self.get_features,
                   "learning_context": self.get_context}
        seen = set()
        for ref in [r for o in record.observations for r in o.evidence] + (record.examples or []):
            key = (ref.record_type, ref.record_id)
            if key in seen:
                continue
            seen.add(key)
            target = methods[ref.record_type](ref.record_id)
            if target is None:
                raise RepositoryConflict("Missing same-channel context evidence.")
            target.require_channel(self._channel)
        return record

    def create_context(self, record):
        """Atomically insert header, ordered children/evidence, then seal forever."""
        if type(record) is not LearningContext:
            raise TypeError("Expected LearningContext.")
        record = LearningContext.model_validate(record.model_dump()).require_channel(self._channel)
        with self.transaction():
            record = self.validate_context_evidence(record)
            columns = to_columns(record)
            columns.pop("observations")
            columns.pop("examples")
            columns["examples_present"] = record.examples is not None
            query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                self._table(LearningContext), sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in columns))
            with self._connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(query, tuple(columns.values()))
                for position, observation in enumerate(record.observations):
                    cursor.execute(sql.SQL("INSERT INTO {} (channel_context_id, context_id, observation_position, notes, sample_count, uncertainty_notes) VALUES (%s,%s,%s,%s,%s,%s)").format(sql.Identifier(self._schema,"learning_observations")),
                        (self._channel,record.context_id,position,observation.notes,observation.sample_count,observation.uncertainty_notes))
                    for evidence_position, ref in enumerate(observation.evidence):
                        self._insert_context_evidence(cursor,record.context_id,"observation",position,evidence_position,ref)
                for position, ref in enumerate(record.examples or []):
                    self._insert_context_evidence(cursor,record.context_id,"example",None,position,ref)
                cursor.execute(sql.SQL("UPDATE {} SET sealed=TRUE WHERE channel_context_id=%s AND context_id=%s AND NOT sealed").format(self._table(LearningContext)),(self._channel,record.context_id))
            return record

    def _insert_context_evidence(self,cursor,context_id,role,observation_position,position,ref):
        cursor.execute(sql.SQL("INSERT INTO {} (channel_context_id,context_id,role,observation_position,evidence_position,record_type,record_id) VALUES (%s,%s,%s,%s,%s,%s,%s)").format(sql.Identifier(self._schema,"learning_evidence")),
            (self._channel,context_id,role,observation_position,position,ref.record_type,ref.record_id))

    def _read_context(self,row):
        """Reconstruct ordered model children; reject gaps and invalid roles."""
        values = dict(row)
        present = values.pop("examples_present")
        if not values.pop("sealed") or values["channel_context_id"] != self._channel:
            raise RepositoryError("Unsealed or wrong-channel context.")
        with self._connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql.SQL("SELECT channel_context_id,context_id,observation_position,notes,sample_count,uncertainty_notes FROM {} WHERE channel_context_id=%s AND context_id=%s ORDER BY observation_position").format(sql.Identifier(self._schema,"learning_observations")),(self._channel,values["context_id"]))
            observations = cursor.fetchall()
            cursor.execute(sql.SQL("SELECT channel_context_id,context_id,role,observation_position,evidence_position,record_type,record_id FROM {} WHERE channel_context_id=%s AND context_id=%s ORDER BY role,observation_position NULLS FIRST,evidence_position").format(sql.Identifier(self._schema,"learning_evidence")),(self._channel,values["context_id"]))
            evidence = cursor.fetchall()
        grouped = {}
        for ref in evidence:
            if ref["channel_context_id"] != self._channel or ref["context_id"] != values["context_id"]:
                raise RepositoryError("Invalid context evidence ownership.")
            key = (ref["role"],ref["observation_position"])
            if key[0] not in {"observation","example"} or (key[0]=="example" and key[1] is not None):
                raise RepositoryError("Invalid context evidence role.")
            items = grouped.setdefault(key,[])
            if ref["evidence_position"] != len(items):
                raise RepositoryError("Invalid context evidence ordering.")
            items.append({name:ref[name] for name in ("channel_context_id","record_type","record_id")})
        values["observations"] = []
        for position, observation in enumerate(observations):
            if observation["observation_position"] != position or observation["channel_context_id"] != self._channel or observation["context_id"] != values["context_id"]:
                raise RepositoryError("Invalid context observation ordering.")
            values["observations"].append(LearningObservation(
                **{name:observation[name] for name in ("notes","sample_count","uncertainty_notes")},
                evidence=grouped.pop(("observation",position),[])).model_dump())
        values["examples"] = grouped.pop(("example",None),[]) if present else None
        if grouped or not values["observations"]:
            raise RepositoryError("Invalid context child structure.")
        return from_row(LearningContext,values,channel_context_id=self._channel)

    def get_context(self, context_id):
        return self._get(LearningContext, "context_id", context_id)

    def list_contexts(self, *, start_at=None, end_at=None, limit=100, offset=0):
        return self._select(LearningContext, time_column="built_at", start_at=start_at,
                            end_at=end_at, order=(("built_at", "DESC"), ("context_id", "ASC")), limit=limit, offset=offset)

    def latest_context(self):
        """Newest build, including expired builds; freshness is returned, not inferred."""
        records = self.list_contexts(limit=1)
        return records[0] if records else None
