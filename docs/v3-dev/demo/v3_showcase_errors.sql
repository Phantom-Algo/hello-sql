CREATE INDEX __sys_bad ON events (id);
CREATE INDEX idx_events_nocol ON events (nocol);
CREATE INDEX idx_events_id ON events (id);
DROP INDEX nope;
CREATE INDEX idx_events_id2 ON nosuch (id);
