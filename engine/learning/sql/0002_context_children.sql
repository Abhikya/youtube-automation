-- 0002: ordered context children and constrained same-channel evidence.
-- Existing JSON contexts are backfilled atomically; unresolved legacy evidence
-- fails its foreign key rather than being silently discarded or guessed.
ALTER TABLE learning_contexts ADD COLUMN examples_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE learning_contexts ADD COLUMN sealed BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE learning_contexts SET examples_present = examples IS NOT NULL;

CREATE TABLE learning_observations (
    channel_context_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    observation_position INTEGER NOT NULL CHECK (observation_position >= 0),
    notes TEXT NOT NULL CHECK (notes ~ '\S'),
    sample_count BIGINT NOT NULL CHECK (sample_count > 0),
    uncertainty_notes TEXT,
    PRIMARY KEY (channel_context_id, context_id, observation_position),
    FOREIGN KEY (channel_context_id, context_id) REFERENCES learning_contexts(channel_context_id, context_id)
);

CREATE TABLE learning_evidence (
    channel_context_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('observation', 'example')),
    observation_position INTEGER,
    evidence_position INTEGER NOT NULL CHECK (evidence_position >= 0),
    record_type TEXT NOT NULL CHECK (record_type IN ('channel_profile','generation','published_video','metric_snapshot','content_features','learning_context')),
    record_id TEXT NOT NULL CHECK (record_id ~ '^\S+$'),
    CHECK ((role = 'observation' AND observation_position IS NOT NULL AND observation_position >= 0)
        OR (role = 'example' AND observation_position IS NULL)),
    CHECK (record_type <> 'channel_profile' OR record_id = channel_context_id),
    profile_id TEXT GENERATED ALWAYS AS (CASE WHEN record_type='channel_profile' THEN record_id END) STORED,
    generation_id TEXT GENERATED ALWAYS AS (CASE WHEN record_type='generation' THEN record_id END) STORED,
    video_id TEXT GENERATED ALWAYS AS (CASE WHEN record_type='published_video' THEN record_id END) STORED,
    snapshot_id TEXT GENERATED ALWAYS AS (CASE WHEN record_type='metric_snapshot' THEN record_id END) STORED,
    feature_id TEXT GENERATED ALWAYS AS (CASE WHEN record_type='content_features' THEN record_id END) STORED,
    referenced_context_id TEXT GENERATED ALWAYS AS (CASE WHEN record_type='learning_context' THEN record_id END) STORED,
    FOREIGN KEY (channel_context_id, context_id) REFERENCES learning_contexts(channel_context_id, context_id),
    FOREIGN KEY (channel_context_id, context_id, observation_position) REFERENCES learning_observations(channel_context_id, context_id, observation_position),
    FOREIGN KEY (profile_id) REFERENCES channel_profiles(channel_context_id),
    FOREIGN KEY (channel_context_id, generation_id) REFERENCES generation_records(channel_context_id,generation_id),
    FOREIGN KEY (channel_context_id, video_id) REFERENCES published_videos(channel_context_id,published_video_id),
    FOREIGN KEY (channel_context_id, snapshot_id) REFERENCES metric_snapshots(channel_context_id,snapshot_id),
    FOREIGN KEY (channel_context_id, feature_id) REFERENCES content_features(channel_context_id,feature_record_id),
    FOREIGN KEY (channel_context_id, referenced_context_id) REFERENCES learning_contexts(channel_context_id,context_id)
);
CREATE UNIQUE INDEX learning_evidence_order ON learning_evidence(channel_context_id,context_id,role,COALESCE(observation_position,-1),evidence_position);

INSERT INTO learning_observations(channel_context_id,context_id,observation_position,notes,sample_count,uncertainty_notes)
SELECT c.channel_context_id,c.context_id,(o.ordinality-1)::integer,o.value->>'notes',(o.value->>'sample_count')::bigint,o.value->>'uncertainty_notes'
FROM learning_contexts c CROSS JOIN LATERAL jsonb_array_elements(c.observations) WITH ORDINALITY o;
INSERT INTO learning_evidence(channel_context_id,context_id,role,observation_position,evidence_position,record_type,record_id)
SELECT e.value->>'channel_context_id',c.context_id,'observation',(o.ordinality-1)::integer,(e.ordinality-1)::integer,e.value->>'record_type',e.value->>'record_id'
FROM learning_contexts c CROSS JOIN LATERAL jsonb_array_elements(c.observations) WITH ORDINALITY o
CROSS JOIN LATERAL jsonb_array_elements(o.value->'evidence') WITH ORDINALITY e;
INSERT INTO learning_evidence(channel_context_id,context_id,role,observation_position,evidence_position,record_type,record_id)
SELECT e.value->>'channel_context_id',c.context_id,'example',NULL,(e.ordinality-1)::integer,e.value->>'record_type',e.value->>'record_id'
FROM learning_contexts c CROSS JOIN LATERAL jsonb_array_elements(c.examples) WITH ORDINALITY e;

-- Verify legacy nested ownership explicitly, even when another context ID matches.
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM learning_contexts c CROSS JOIN LATERAL jsonb_array_elements(c.observations) o
 CROSS JOIN LATERAL jsonb_array_elements(o->'evidence') e WHERE (e->>'channel_context_id') IS DISTINCT FROM c.channel_context_id)
 OR EXISTS (SELECT 1 FROM learning_contexts c CROSS JOIN LATERAL jsonb_array_elements(c.examples) e WHERE (e->>'channel_context_id') IS DISTINCT FROM c.channel_context_id)
 THEN RAISE EXCEPTION 'Invalid legacy evidence channel'; END IF;
END $$;
-- Reject malformed legacy contexts before dropping their JSON representation.
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM learning_contexts c WHERE NOT EXISTS
    (SELECT 1 FROM learning_observations o WHERE o.channel_context_id=c.channel_context_id AND o.context_id=c.context_id))
 OR EXISTS (SELECT 1 FROM learning_observations o WHERE NOT EXISTS
    (SELECT 1 FROM learning_evidence e WHERE e.channel_context_id=o.channel_context_id AND e.context_id=o.context_id AND e.observation_position=o.observation_position AND e.role='observation'))
 THEN RAISE EXCEPTION 'Invalid legacy context structure'; END IF;
END $$;
ALTER TABLE learning_contexts DROP COLUMN observations, DROP COLUMN examples;
UPDATE learning_contexts SET sealed=TRUE;

CREATE FUNCTION guard_context_header() RETURNS TRIGGER LANGUAGE plpgsql SET search_path FROM CURRENT AS $$ BEGIN
 IF TG_OP='INSERT' THEN
   IF NEW.sealed THEN RAISE EXCEPTION 'New context must be unsealed'; END IF;
   RETURN NEW;
 END IF;
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Contexts are immutable'; END IF;
 IF OLD.sealed OR NOT NEW.sealed OR (to_jsonb(NEW)-'sealed') IS DISTINCT FROM (to_jsonb(OLD)-'sealed')
 THEN RAISE EXCEPTION 'Contexts are immutable'; END IF;
 IF NOT EXISTS (SELECT 1 FROM learning_observations o WHERE o.channel_context_id=NEW.channel_context_id AND o.context_id=NEW.context_id)
 OR EXISTS (SELECT 1 FROM learning_observations o WHERE o.channel_context_id=NEW.channel_context_id AND o.context_id=NEW.context_id AND
   (o.sample_count>NEW.sample_count OR NOT EXISTS (SELECT 1 FROM learning_evidence e WHERE e.channel_context_id=o.channel_context_id AND e.context_id=o.context_id AND e.role='observation' AND e.observation_position=o.observation_position)))
 THEN RAISE EXCEPTION 'Invalid context children'; END IF;
 IF EXISTS (SELECT 1 FROM learning_observations o WHERE o.channel_context_id=NEW.channel_context_id AND o.context_id=NEW.context_id
   GROUP BY o.channel_context_id,o.context_id HAVING MIN(observation_position)<>0 OR MAX(observation_position)<>COUNT(*)-1)
 OR EXISTS (SELECT 1 FROM learning_evidence e WHERE e.channel_context_id=NEW.channel_context_id AND e.context_id=NEW.context_id
   GROUP BY role,observation_position HAVING MIN(evidence_position)<>0 OR MAX(evidence_position)<>COUNT(*)-1)
 OR (NOT NEW.examples_present AND EXISTS (SELECT 1 FROM learning_evidence e WHERE e.channel_context_id=NEW.channel_context_id AND e.context_id=NEW.context_id AND role='example'))
 THEN RAISE EXCEPTION 'Invalid context ordering'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER context_header_immutable BEFORE INSERT OR UPDATE OR DELETE ON learning_contexts FOR EACH ROW EXECUTE FUNCTION guard_context_header();
CREATE FUNCTION guard_context_child() RETURNS TRIGGER LANGUAGE plpgsql SET search_path FROM CURRENT AS $$ BEGIN
 IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Context children are immutable'; END IF;
 PERFORM 1 FROM learning_contexts WHERE channel_context_id=NEW.channel_context_id AND context_id=NEW.context_id AND NOT sealed FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Context is sealed or missing'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER observations_immutable BEFORE INSERT OR UPDATE OR DELETE ON learning_observations FOR EACH ROW EXECUTE FUNCTION guard_context_child();
CREATE TRIGGER evidence_immutable BEFORE INSERT OR UPDATE OR DELETE ON learning_evidence FOR EACH ROW EXECUTE FUNCTION guard_context_child();

-- An unsealed partial header cannot be committed by a direct SQL caller either.
CREATE FUNCTION require_sealed_context() RETURNS TRIGGER LANGUAGE plpgsql SET search_path FROM CURRENT AS $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM learning_contexts WHERE channel_context_id=NEW.channel_context_id AND context_id=NEW.context_id AND sealed)
 THEN RAISE EXCEPTION 'Unprepared context cannot commit'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER context_must_be_sealed AFTER INSERT OR UPDATE ON learning_contexts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_sealed_context();
