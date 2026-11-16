CREATE ROLE `spine_neo4j_sa_rw` IF NOT EXISTS;
GRANT ALL DATABASE PRIVILEGES ON DATABASE `spine` TO `spine_neo4j_sa_rw`;
GRANT ALL GRAPH PRIVILEGES ON GRAPH `spine` TO `spine_neo4j_sa_rw`;
//--------------------------------------
CREATE ROLE `spine_analyst` IF NOT EXISTS;
GRANT ACCESS ON DATABASE `spine` TO `spine_analyst`;

GRANT MATCH {*} ON GRAPH `spine` NODE * TO `spine_analyst`;
GRANT MATCH {*} ON GRAPH `spine` RELATIONSHIP * TO `spine_analyst`;
GRANT SHOW INDEX ON DATABASE `spine` TO `spine_analyst`;
GRANT SHOW CONSTRAINT ON DATABASE `spine` TO `spine_analyst`;

DENY READ { birthDate } ON GRAPH `spine` NODES Person TO `spine_analyst`;
//--------------------------------------
CREATE ROLE `spine_officer` IF NOT EXISTS;
GRANT ACCESS ON DATABASE `spine` TO `spine_officer`;

GRANT MATCH {*} ON GRAPH `spine` NODES * TO `spine_officer`;
GRANT MATCH {*} ON GRAPH `spine` RELATIONSHIPS * TO `spine_officer`;
GRANT SHOW INDEX ON DATABASE `spine` TO `spine_officer`;
GRANT SHOW CONSTRAINT ON DATABASE `spine` TO `spine_officer`;
