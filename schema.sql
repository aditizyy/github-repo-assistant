-- schema.sql — Run this once to initialise the database
-- Demonstrates: table design, indexing, foreign keys

CREATE DATABASE IF NOT EXISTS repo_assistant
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE repo_assistant;

-- Stores every repository a user has submitted
CREATE TABLE IF NOT EXISTS repositories (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    github_url    VARCHAR(500) NOT NULL,
    repo_name     VARCHAR(255) NOT NULL,
    clone_path    VARCHAR(500),
    status        ENUM('pending', 'cloning', 'indexing', 'ready', 'error') DEFAULT 'pending',
    file_count    INT DEFAULT 0,
    chunk_count   INT DEFAULT 0,
    error_message TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_status (status),
    INDEX idx_github_url (github_url(255))
) ENGINE=InnoDB;

-- Chat sessions: one repo can have many chat sessions
CREATE TABLE IF NOT EXISTS chat_sessions (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    repo_id       INT NOT NULL,
    session_name  VARCHAR(255) DEFAULT 'New Chat',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories(id) ON DELETE CASCADE,
    INDEX idx_repo_id (repo_id)
) ENGINE=InnoDB;

-- Every Q&A turn in a session
CREATE TABLE IF NOT EXISTS chat_messages (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    session_id    INT NOT NULL,
    role          ENUM('user', 'assistant') NOT NULL,
    content       MEDIUMTEXT NOT NULL,
    tokens_used   INT DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
    INDEX idx_session_id (session_id)
) ENGINE=InnoDB;

-- Metadata for each indexed file (for search + display)
CREATE TABLE IF NOT EXISTS indexed_files (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    repo_id       INT NOT NULL,
    file_path     VARCHAR(1000) NOT NULL,
    language      VARCHAR(50),
    line_count    INT DEFAULT 0,
    chunk_count   INT DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories(id) ON DELETE CASCADE,
    INDEX idx_repo_id (repo_id)
) ENGINE=InnoDB;