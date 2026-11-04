CREATE TABLE observations(id INTEGER PRIMARY KEY, project TEXT, sdk_session_id TEXT, title TEXT, narrative TEXT, facts TEXT, created_at TEXT, created_at_epoch INTEGER);
INSERT INTO observations VALUES(1,'TEST-project','TEST-chat','TEST palette','TEST assistant remembers saffron.','["saffron"]','2026-01-01T12:00:00Z',1767268800000);
CREATE TABLE session_summaries(id INTEGER PRIMARY KEY, project TEXT, sdk_session_id TEXT, learned TEXT, created_at_epoch INTEGER);
INSERT INTO session_summaries VALUES(1,'TEST-project','TEST-chat','TEST summary saffron.',1767268860000);
