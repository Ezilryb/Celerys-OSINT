-- ============================================================
-- WKD DISCORD BOT - Schéma PostgreSQL
-- Version: 2.4 PRODUCTION
-- ============================================================

-- Extension pour UUID (sécurité IDs)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- TABLE PRINCIPALE : UTILISATEURS
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    user_id             BIGINT PRIMARY KEY,
    username            VARCHAR(100) NOT NULL,
    balance             INTEGER NOT NULL DEFAULT 0
                            CONSTRAINT balance_non_negative CHECK (balance >= 0),
    total_earned        INTEGER NOT NULL DEFAULT 0,
    total_spent         INTEGER NOT NULL DEFAULT 0,
    total_burned        INTEGER NOT NULL DEFAULT 0,
    message_count       INTEGER NOT NULL DEFAULT 0,
    daily_earned        INTEGER NOT NULL DEFAULT 0,
    last_daily_reset    DATE,
    last_message        TIMESTAMP WITH TIME ZONE,
    joined_date         TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    account_created_date TIMESTAMP WITH TIME ZONE,
    server_join_date    TIMESTAMP WITH TIME ZONE,
    airdrop_eligible_date TIMESTAMP WITH TIME ZONE,
    airdrop_received    BOOLEAN NOT NULL DEFAULT FALSE,
    flags               TEXT[] DEFAULT '{}',
    banned              BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================================
-- TABLE : TRANSACTIONS (journal immuable)
-- ============================================================
CREATE TABLE IF NOT EXISTS transactions (
    id          SERIAL PRIMARY KEY,
    tx_id       VARCHAR(50) UNIQUE NOT NULL DEFAULT ('tx_' || encode(gen_random_bytes(8), 'hex')),
    from_user   BIGINT REFERENCES users(user_id) ON DELETE RESTRICT,
    to_user     BIGINT REFERENCES users(user_id) ON DELETE RESTRICT,
    amount      INTEGER NOT NULL CONSTRAINT amount_positive CHECK (amount > 0),
    type        VARCHAR(50) NOT NULL, -- 'contract', 'bet_win', 'tax_inactive', 'tax_rich', 'airdrop', 'admin_give', 'burn', 'escrow_lock', 'escrow_release'
    reason      TEXT,
    reference_id VARCHAR(50),        -- ID du contrat ou pari associé
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
-- Note: Les transactions ne sont JAMAIS supprimées, uniquement annulées via rollback

-- ============================================================
-- TABLE : ROLLBACKS (audit trail)
-- ============================================================
CREATE TABLE IF NOT EXISTS rollbacks (
    id              SERIAL PRIMARY KEY,
    original_tx_id  VARCHAR(50) REFERENCES transactions(tx_id),
    rolled_back_by  BIGINT REFERENCES users(user_id),
    reason          TEXT NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================================
-- TABLE : CONTRATS P2P
-- ============================================================
CREATE TABLE IF NOT EXISTS contracts (
    id              SERIAL PRIMARY KEY,
    contract_id     VARCHAR(50) UNIQUE NOT NULL DEFAULT ('ctr_' || encode(gen_random_bytes(8), 'hex')),
    creator_id      BIGINT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    acceptor_id     BIGINT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    amount_sent     INTEGER NOT NULL CONSTRAINT amount_sent_positive CHECK (amount_sent >= 1),
    amount_received INTEGER NOT NULL CONSTRAINT amount_received_gt_sent CHECK (amount_received >= amount_sent + 1),
    note            TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CONSTRAINT contracts_status_valid CHECK (status IN ('pending', 'accepted', 'completed', 'refused', 'expired', 'cancelled')),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    accepted_at     TIMESTAMP WITH TIME ZONE,
    completed_at    TIMESTAMP WITH TIME ZONE,
    expires_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (NOW() + INTERVAL '48 hours'),
    CONSTRAINT contract_no_self_deal CHECK (creator_id != acceptor_id)
);

-- ============================================================
-- TABLE : PARIS
-- ============================================================
CREATE TABLE IF NOT EXISTS bets (
    id              SERIAL PRIMARY KEY,
    bet_id          VARCHAR(50) UNIQUE NOT NULL DEFAULT ('bet_' || encode(gen_random_bytes(8), 'hex')),
    bettor_a        BIGINT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    bettor_b        BIGINT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    amount          INTEGER NOT NULL CONSTRAINT bet_amount_positive CHECK (amount >= 1),
    condition       TEXT NOT NULL,
    status          VARCHAR(30) NOT NULL DEFAULT 'pending_acceptance'
                        CONSTRAINT bets_status_valid CHECK (status IN (
                            'pending_acceptance', -- En attente que B accepte
                            'pending_jury',       -- En attente validation jury
                            'active',             -- Pari en cours
                            'pending_resolution', -- Réclamation déposée
                            'resolved',           -- Résolu
                            'cancelled',          -- Annulé
                            'refused',            -- Refusé par B
                            'expired'             -- Expiré (48h sans réponse)
                        )),
    escrow_locked   BOOLEAN NOT NULL DEFAULT FALSE,  -- Fonds bloqués en escrow
    winner          BIGINT REFERENCES users(user_id),
    admin_notes     TEXT,
    claim_note      TEXT,                             -- Note lors de la réclamation
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    accepted_at     TIMESTAMP WITH TIME ZONE,
    validated_at    TIMESTAMP WITH TIME ZONE,
    claimed_at      TIMESTAMP WITH TIME ZONE,
    resolved_at     TIMESTAMP WITH TIME ZONE,
    expires_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (NOW() + INTERVAL '48 hours'),
    CONSTRAINT bet_no_self_bet CHECK (bettor_a != bettor_b)
);

-- ============================================================
-- TABLE : ESCROW (fonds bloqués pour les paris)
-- ============================================================
CREATE TABLE IF NOT EXISTS escrow (
    id          SERIAL PRIMARY KEY,
    bet_id      VARCHAR(50) UNIQUE NOT NULL REFERENCES bets(bet_id),
    user_a_amount INTEGER NOT NULL,
    user_b_amount INTEGER NOT NULL,
    locked_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    released_at TIMESTAMP WITH TIME ZONE,
    released_to BIGINT REFERENCES users(user_id)
);

-- ============================================================
-- TABLE : POOL JURY
-- ============================================================
CREATE TABLE IF NOT EXISTS jury_pool (
    user_id     BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    added_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    added_by    BIGINT REFERENCES users(user_id),
    active      BOOLEAN NOT NULL DEFAULT TRUE
);

-- ============================================================
-- TABLE : VOTES JURY
-- ============================================================
CREATE TABLE IF NOT EXISTS jury_votes (
    id              SERIAL PRIMARY KEY,
    bet_id          VARCHAR(50) NOT NULL REFERENCES bets(bet_id),
    juror_id        BIGINT NOT NULL REFERENCES users(user_id),
    vote            VARCHAR(20) CONSTRAINT vote_valid CHECK (vote IN ('approve', 'reject', 'continue', NULL)),
    vote_phase      VARCHAR(20) NOT NULL DEFAULT 'validation'
                        CONSTRAINT vote_phase_valid CHECK (vote_phase IN ('validation', 'resolution')),
    is_replacement  BOOLEAN NOT NULL DEFAULT FALSE,
    replacement_of  BIGINT REFERENCES users(user_id),
    vote_deadline   TIMESTAMP WITH TIME ZONE NOT NULL,
    voted_at        TIMESTAMP WITH TIME ZONE,
    replaced        BOOLEAN NOT NULL DEFAULT FALSE,
    notified_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(bet_id, juror_id, vote_phase)
);

-- ============================================================
-- TABLE : FOND ADMINISTRATEUR
-- ============================================================
CREATE TABLE IF NOT EXISTS admin_fund (
    id                  SERIAL PRIMARY KEY,
    balance             INTEGER NOT NULL DEFAULT 10000 CONSTRAINT fund_balance_check CHECK (balance >= 0),
    initial_supply      INTEGER NOT NULL DEFAULT 10000,
    total_created       INTEGER NOT NULL DEFAULT 10000,
    total_distributed   INTEGER NOT NULL DEFAULT 0,
    max_supply          INTEGER NOT NULL DEFAULT 111111,
    last_updated        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
-- Initialiser le fond au démarrage si vide
INSERT INTO admin_fund (balance, initial_supply, total_created, total_distributed, max_supply)
SELECT 10000, 10000, 10000, 0, 111111
WHERE NOT EXISTS (SELECT 1 FROM admin_fund);

-- ============================================================
-- TABLE : CONFIGURATION DYNAMIQUE
-- ============================================================
CREATE TABLE IF NOT EXISTS config (
    key         VARCHAR(100) PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_by  BIGINT REFERENCES users(user_id)
);
-- Valeurs par défaut
INSERT INTO config (key, value, description) VALUES
    ('message_reward_count',    '222',      'Nb messages pour gagner 1 WKD'),
    ('message_reward_amount',   '1',        'WKD gagnés par palier'),
    ('daily_limit',             '3',        'WKD max gagnable par jour via messages'),
    ('message_cooldown_seconds','60',       'Délai anti-spam entre messages comptés'),
    ('airdrop_amount',          '111',      'WKD donnés au nouvel airdrop'),
    ('airdrop_delay_days',      '15',       'Jours avant airdrop nouveaux membres'),
    ('inactive_tax_rate',       '0.01',     'Taxe inactivité (1%)'),
    ('inactive_tax_min_balance','222',      'Solde minimum pour taxe inactivité'),
    ('inactive_days_threshold', '7',        'Jours sans message pour être inactif'),
    ('rich_tax_rate',           '0.005',    'Taxe riches (0.5%)'),
    ('rich_tax_top_n',          '9',        'Nombre de riches taxés'),
    ('contract_cooldown_hours', '24',       'Cooldown contrats entre même paire'),
    ('bet_cooldown_hours',      '48',       'Cooldown paris par membre'),
    ('bet_expiry_hours',        '48',       'Expiration offre de pari'),
    ('contract_expiry_hours',   '48',       'Expiration offre de contrat'),
    ('jury_pool_max',           '8',        'Taille maximale pool jury'),
    ('jury_vote_initial_hours', '24',       'Délai initial vote jury'),
    ('jury_vote_replacement_hours', '12',   'Délai remplacement vote jury'),
    ('blockchain_locked',       'false',    'Blocage global de la blockchain'),
    ('blockchain_lock_reason',  '',         'Raison du blocage blockchain'),
    ('min_account_age_days',    '30',       'Âge minimum compte Discord'),
    ('min_server_age_days',     '15',       'Ancienneté minimale sur le serveur')
ON CONFLICT (key) DO NOTHING;

-- ============================================================
-- TABLE : AUDIT LOG (actions admin)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    actor_id    BIGINT REFERENCES users(user_id),
    action      VARCHAR(100) NOT NULL,
    target_id   BIGINT,               -- User cible si applicable
    details     JSONB DEFAULT '{}',
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================================
-- TABLE : EXEMPTIONS (audit)
-- ============================================================
CREATE TABLE IF NOT EXISTS exemption_audit (
    id              SERIAL PRIMARY KEY,
    exempted_user   BIGINT REFERENCES users(user_id),
    exempted_by     BIGINT REFERENCES users(user_id),
    reason          TEXT,
    exempted_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================================
-- TABLE : FLAGS (multi-comptes, comportements suspects)
-- ============================================================
CREATE TABLE IF NOT EXISTS user_flags (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(user_id),
    flag_type   VARCHAR(50) NOT NULL, -- 'multi_account', 'circular_tx', 'spam', 'suspicious_timing'
    details     TEXT,
    severity    VARCHAR(10) NOT NULL DEFAULT 'low'
                    CONSTRAINT severity_valid CHECK (severity IN ('low', 'medium', 'high')),
    resolved    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE,
    resolved_by BIGINT REFERENCES users(user_id)
);

-- ============================================================
-- VUE PUBLIQUE : BLOCKCHAIN (#transactions)
-- ============================================================
CREATE OR REPLACE VIEW public_transactions AS
SELECT
    t.tx_id,
    t.type,
    fu.username AS from_username,
    tu.username AS to_username,
    t.amount,
    t.reason,
    t.reference_id,
    t.created_at
FROM transactions t
LEFT JOIN users fu ON t.from_user = fu.user_id
LEFT JOIN users tu ON t.to_user = tu.user_id
ORDER BY t.created_at DESC;

-- ============================================================
-- INDEXES (performances)
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_users_balance      ON users(balance DESC);
CREATE INDEX IF NOT EXISTS idx_users_airdrop      ON users(airdrop_eligible_date) WHERE airdrop_received = FALSE;
CREATE INDEX IF NOT EXISTS idx_users_banned       ON users(banned) WHERE banned = TRUE;
CREATE INDEX IF NOT EXISTS idx_transactions_from  ON transactions(from_user, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_to    ON transactions(to_user, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_type  ON transactions(type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contracts_status   ON contracts(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_contracts_pair     ON contracts(creator_id, acceptor_id);
CREATE INDEX IF NOT EXISTS idx_bets_status        ON bets(status);
CREATE INDEX IF NOT EXISTS idx_jury_votes_bet     ON jury_votes(bet_id);
CREATE INDEX IF NOT EXISTS idx_jury_votes_deadline ON jury_votes(vote_deadline) WHERE replaced = FALSE AND voted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_flags_user         ON user_flags(user_id, resolved);
CREATE INDEX IF NOT EXISTS idx_audit_actor        ON audit_log(actor_id, created_at DESC);

-- ============================================================
-- FONCTION : Trigger mise à jour updated_at
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ============================================================
-- FONCTION : Vérification intégrité solde (jamais négatif)
-- ============================================================
CREATE OR REPLACE FUNCTION check_balance_integrity()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.balance < 0 THEN
        RAISE EXCEPTION 'INTEGRITY_ERROR: Balance cannot go negative for user %', NEW.user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_balance_integrity
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION check_balance_integrity();
