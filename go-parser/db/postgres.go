package db

import (
	"database/sql"
	"fmt"
	_ "github.com/lib/pq"
	"ivd_analyzer_go/config"
	"regexp"
	"strings"
	"sync"
	"time"
)

var DB *sql.DB

var sanitizeRe = regexp.MustCompile(`[^a-zA-Z0-9_]`)

func Init(cfg *config.Config) error {
	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		cfg.DBHost, cfg.DBPort, cfg.DBUser, cfg.DBPassword, cfg.DBName)
	var err error
	DB, err = sql.Open("postgres", connStr)
	if err != nil {
		return err
	}
	DB.SetMaxOpenConns(20)
	DB.SetMaxIdleConns(5)
	DB.SetConnMaxLifetime(30 * time.Minute)
	return DB.Ping()
}

type Rule struct {
	ID       int
	Keywords string
	Advice   string
	Source   string
}

type MotorStatus struct {
	BoardCard       string
	MotorCode       string
	StatusCode      string
	MotorName       string
	ActionType      string
	TargetValue     string
	Sensor          string
	Description     string
	FullDescription string
}

var (
	rulesCache   = make(map[string][]Rule)
	rulesCacheMu sync.RWMutex
	rulesCacheT  = make(map[string]time.Time)
	rulesCacheTTL = 5 * time.Minute
)

func GetRules(series, model string) ([]Rule, error) {
	key := series + ":" + model
	rulesCacheMu.RLock()
	if cached, ok := rulesCache[key]; ok {
		if t, ok2 := rulesCacheT[key]; ok2 && time.Since(t) < rulesCacheTTL {
			rulesCacheMu.RUnlock()
			return cached, nil
		}
	}
	rulesCacheMu.RUnlock()

	rows, err := DB.Query(`
		SELECT r.id, r.keywords, r.advice, r.source
		FROM rules r
		JOIN models m ON r.model_id = m.id
		JOIN series s ON m.series_id = s.id
		WHERE s.name = $1 AND m.name = $2
	`, series, model)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var rules []Rule
	for rows.Next() {
		var r Rule
		err := rows.Scan(&r.ID, &r.Keywords, &r.Advice, &r.Source)
		if err != nil {
			return nil, err
		}
		rules = append(rules, r)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	rulesCacheMu.Lock()
	rulesCache[key] = rules
	rulesCacheT[key] = time.Now()
	rulesCacheMu.Unlock()

	return rules, nil
}

func sanitizeTableName(model string) string {
	clean := sanitizeRe.ReplaceAllString(model, "")
	if clean == "" {
		return ""
	}
	return "motor_status_" + strings.ToLower(clean)
}

func LookupMotorStatus(board, motor, status, model string) (*MotorStatus, error) {
	results, err := LookupMotorStatusBatch([][3]string{{board, motor, status}}, model)
	if err != nil {
		return nil, err
	}
	if ms, ok := results[[3]string{board, motor, status}]; ok {
		return ms, nil
	}
	return nil, nil
}

func LookupMotorStatusBatch(codes [][3]string, model string) (map[[3]string]*MotorStatus, error) {
	tableName := sanitizeTableName(model)
	if tableName == "" {
		return nil, fmt.Errorf("invalid model name: %s", model)
	}
	if len(codes) == 0 {
		return map[[3]string]*MotorStatus{}, nil
	}
	placeholders := make([]string, 0, len(codes))
	params := make([]interface{}, 0, len(codes)*3)
	for i, code := range codes {
		placeholders = append(placeholders, fmt.Sprintf("($%d,$%d,$%d)", i*3+1, i*3+2, i*3+3))
		params = append(params, code[0], code[1], code[2])
	}
	query := fmt.Sprintf(`
        SELECT board_card, motor_code, status_code, motor_name, action_type, target_value, sensor, description, full_description
        FROM %s
        WHERE (board_card, motor_code, status_code) IN (%s)
    `, tableName, strings.Join(placeholders, ","))
	rows, err := DB.Query(query, params...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	results := make(map[[3]string]*MotorStatus)
	for rows.Next() {
		var ms MotorStatus
		if err := rows.Scan(&ms.BoardCard, &ms.MotorCode, &ms.StatusCode, &ms.MotorName, &ms.ActionType, &ms.TargetValue, &ms.Sensor, &ms.Description, &ms.FullDescription); err != nil {
			return nil, err
		}
		key := [3]string{ms.BoardCard, ms.MotorCode, ms.StatusCode}
		results[key] = &ms
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return results, nil
}
