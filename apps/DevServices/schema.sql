CREATE DATABASE IF NOT EXISTS fate_test
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE fate_test;

CREATE TABLE IF NOT EXISTS cc (
  guild_id BIGINT UNSIGNED NOT NULL,
  command VARCHAR(64) NOT NULL,
  response TEXT NOT NULL,
  PRIMARY KEY (guild_id, command)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS global_msg (
  user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  xp BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS global_monthly (
  user_id BIGINT UNSIGNED NOT NULL,
  timeframe DOUBLE NOT NULL,
  xp BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, timeframe),
  INDEX global_monthly_timeframe_idx (timeframe)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS msg (
  guild_id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  xp BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id),
  INDEX msg_user_idx (user_id),
  INDEX msg_xp_idx (guild_id, xp)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS monthly_msg (
  guild_id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  msg_time DOUBLE NOT NULL,
  xp BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id, msg_time),
  INDEX monthly_msg_time_idx (msg_time)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS commands (
  command VARCHAR(128) NOT NULL,
  total BIGINT NOT NULL DEFAULT 0,
  ran_at DOUBLE NOT NULL,
  PRIMARY KEY (command, ran_at),
  INDEX commands_ran_at_idx (ran_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS role_rewards (
  guild_id BIGINT UNSIGNED NOT NULL,
  role_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  lvl INT NOT NULL,
  stack BOOLEAN NOT NULL DEFAULT FALSE,
  INDEX role_rewards_guild_idx (guild_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS privacy (
  user_id BIGINT UNSIGNED NOT NULL,
  item VARCHAR(64) NOT NULL,
  value VARCHAR(255) NULL,
  PRIMARY KEY (user_id, item)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS votes (
  user_id BIGINT UNSIGNED NOT NULL,
  vote_time DOUBLE NOT NULL,
  INDEX votes_user_idx (user_id),
  INDEX votes_time_idx (vote_time)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS blocked (
  user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  name VARCHAR(255) NULL,
  reason TEXT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS cases (
  guild_id BIGINT UNSIGNED NOT NULL,
  user_id VARCHAR(32) NULL,
  case_action VARCHAR(64) NOT NULL,
  reason TEXT NULL,
  link TEXT NULL,
  case_number INT NOT NULL,
  created_by VARCHAR(32) NULL,
  created_at DOUBLE NOT NULL,
  PRIMARY KEY (guild_id, case_number),
  INDEX cases_user_idx (user_id),
  INDEX cases_created_at_idx (created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS anti_spam_mutes (
  guild_id BIGINT UNSIGNED NOT NULL,
  channel_id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  mute_role_id BIGINT UNSIGNED NOT NULL,
  end_time DOUBLE NOT NULL,
  PRIMARY KEY (guild_id, user_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS modmail (
  guild_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  channel_id BIGINT UNSIGNED NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS invites (
  code VARCHAR(32) NOT NULL PRIMARY KEY,
  guild_id BIGINT UNSIGNED NOT NULL,
  guild_name TEXT NULL,
  channel_id BIGINT UNSIGNED NULL,
  channel_name TEXT NULL,
  inviter BIGINT UNSIGNED NULL,
  uses INT NOT NULL DEFAULT 0,
  created_at DOUBLE NULL,
  deleted_at DOUBLE NULL,
  INDEX invites_guild_idx (guild_id),
  INDEX invites_created_idx (created_at),
  INDEX invites_deleted_idx (deleted_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS usernames (
  user_id BIGINT UNSIGNED NOT NULL,
  username TEXT NOT NULL,
  changed_at DOUBLE NOT NULL,
  INDEX usernames_user_idx (user_id),
  INDEX usernames_changed_idx (changed_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS activity (
  user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  last_online VARCHAR(64) NULL,
  last_message VARCHAR(64) NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS snipe (
  guild_id BIGINT UNSIGNED NOT NULL PRIMARY KEY
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS battles (
  winner BIGINT UNSIGNED NOT NULL,
  loser BIGINT UNSIGNED NOT NULL,
  INDEX battles_winner_idx (winner),
  INDEX battles_loser_idx (loser)
) ENGINE=InnoDB;
