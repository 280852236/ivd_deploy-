package db

import (
    "database/sql"
    "fmt"
    _ "github.com/lib/pq"
    "ivd_analyzer_go/config"
)

var DB *sql.DB

func Init(cfg *config.Config) error {
    connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
        cfg.DBHost, cfg.DBPort, cfg.DBUser, cfg.DBPassword, cfg.DBName)
    var err error
    DB, err = sql.Open("postgres", connStr)
    if err != nil {
        return err
    }
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

func GetRules(series, model string) ([]Rule, error) {
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
    return rules, nil
}

func LookupMotorStatus(board, motor, status, model string) (*MotorStatus, error) {
    tableName := fmt.Sprintf("motor_status_%s", model)
    row := DB.QueryRow(fmt.Sprintf(`
        SELECT board_card, motor_code, status_code, motor_name, action_type, target_value, sensor, description, full_description
        FROM %s
        WHERE board_card = $1 AND motor_code = $2 AND status_code = $3
    `, tableName), board, motor, status)
    var ms MotorStatus
    err := row.Scan(&ms.BoardCard, &ms.MotorCode, &ms.StatusCode, &ms.MotorName, &ms.ActionType, &ms.TargetValue, &ms.Sensor, &ms.Description, &ms.FullDescription)
    if err == sql.ErrNoRows {
        return nil, nil
    }
    if err != nil {
        return nil, err
    }
    return &ms, nil
}
