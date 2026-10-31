CREATE TABLE users (
	id INTEGER NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	telegram_chat_id VARCHAR(64), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE TABLE jobs (
	id INTEGER NOT NULL, 
	source VARCHAR(30) NOT NULL, 
	external_id VARCHAR(100) NOT NULL, 
	company VARCHAR(120) NOT NULL, 
	title VARCHAR(300) NOT NULL, 
	location VARCHAR(200) NOT NULL, 
	url VARCHAR(600) NOT NULL, 
	description TEXT NOT NULL, 
	sector VARCHAR(60), 
	remote BOOLEAN, 
	job_type VARCHAR(20), 
	country VARCHAR(160), 
	salary_min FLOAT, 
	salary_max FLOAT, 
	first_seen DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (source, external_id)
);
CREATE INDEX ix_jobs_company ON jobs (company);
CREATE TABLE profiles (
	user_id INTEGER NOT NULL, 
	phone VARCHAR(32) NOT NULL, 
	college VARCHAR(200) NOT NULL, 
	grad_year INTEGER, 
	branch VARCHAR(80) NOT NULL, 
	cgpa FLOAT, 
	skills JSON NOT NULL, 
	target_roles JSON NOT NULL, 
	watch_companies JSON NOT NULL, 
	min_score INTEGER NOT NULL, 
	min_lpa FLOAT, 
	max_lpa FLOAT, 
	require_salary BOOLEAN, 
	sectors JSON, 
	experience_years FLOAT, 
	countries JSON, 
	remote_pref VARCHAR(30), 
	job_types JSON, 
	degree VARCHAR(80), 
	linkedin_url VARCHAR(300), 
	github_url VARCHAR(300), 
	portfolio_url VARCHAR(300), 
	city VARCHAR(120), 
	tenth_pct FLOAT, 
	twelfth_pct FLOAT, 
	work_auth VARCHAR(200), 
	gmail_address VARCHAR(255), 
	gmail_app_password_enc TEXT, 
	resume_text TEXT NOT NULL, 
	resume_path VARCHAR(500) NOT NULL, 
	PRIMARY KEY (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE TABLE matches (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	job_id INTEGER NOT NULL, 
	score INTEGER NOT NULL, 
	eligible BOOLEAN NOT NULL, 
	matched_skills JSON NOT NULL, 
	missing_skills JSON NOT NULL, 
	reason TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (user_id, job_id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id)
);
CREATE INDEX ix_matches_job_id ON matches (job_id);
CREATE INDEX ix_matches_user_id ON matches (user_id);
CREATE TABLE applications (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	job_id INTEGER NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	notes TEXT NOT NULL, 
	applied_at DATETIME, 
	follow_up_at DATETIME, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (user_id, job_id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id)
);
CREATE INDEX ix_applications_user_id ON applications (user_id);
CREATE TABLE contacts (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	company VARCHAR(120) NOT NULL, 
	link VARCHAR(400) NOT NULL, 
	notes TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_contacts_user_id ON contacts (user_id);
CREATE INDEX ix_contacts_company ON contacts (company);
CREATE TABLE email_events (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	message_id VARCHAR(300) NOT NULL, 
	application_id INTEGER, 
	status VARCHAR(30) NOT NULL, 
	subject VARCHAR(300) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (user_id, message_id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_email_events_user_id ON email_events (user_id);
