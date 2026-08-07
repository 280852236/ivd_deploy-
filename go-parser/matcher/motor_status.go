package matcher

import (
    "fmt"
    "ivd_analyzer_go/db"
    "strings"
)

type MotorMatch struct {
    Type            string   `json:"type"`
    BoardCard       string   `json:"board_card"`
    MotorCode       string   `json:"motor_code"`
    StatusCode      string   `json:"status_code"`
    MotorName       string   `json:"motor_name"`
    ActionType      string   `json:"action_type"`
    TargetValue     string   `json:"target_value"`
    Sensor          string   `json:"sensor"`
    Description     string   `json:"description"`
    FullDescription string   `json:"full_description"`
    Diagnosis       string   `json:"diagnosis"`
    Command         string   `json:"command"`
    Keywords        []string `json:"keywords"`
    Advice          string   `json:"advice"`
    Source          string   `json:"source"`
    OriginalText    string   `json:"original_text"`
    EventTime       string   `json:"event_time"`
    EventDate       string   `json:"event_date"`
    RawHex          string   `json:"raw_hex,omitempty"`
    Unmatched       bool     `json:"unmatched,omitempty"`
}

func FindMotorStatusMatches(text, model string) []MotorMatch {
    var matches []MotorMatch

    type codeEntry struct {
        board, motor, status, hex, originalText string
        pos                                     int
    }
    var entries []codeEntry
    var codes [][3]string
    codeSeen := make(map[[3]string]bool)

    for _, match := range longHexPattern.FindAllStringSubmatch(text, -1) {
        hex := strings.ToUpper(match[1])
        if len(hex) >= 12 {
            groups := make([]string, 0, len(hex)/2)
            for i := 0; i+1 < len(hex); i += 2 {
                groups = append(groups, hex[i:i+2])
            }
            if len(groups) < 4 {
                continue
            }
            board := groups[0]
            motor := groups[2]
            status := groups[3]
            key := [3]string{board, motor, status}
            if codeSeen[key] {
                continue
            }
            codeSeen[key] = true
            idx := strings.Index(text, match[0])
            if idx == -1 {
                idx = 0
            }
            entries = append(entries, codeEntry{board: board, motor: motor, status: status, hex: hex, pos: idx})
            codes = append(codes, key)
        }
    }

    for _, match := range triplePattern.FindAllStringSubmatch(text, -1) {
        board := strings.ToUpper(match[1])
        motor := strings.ToUpper(match[2])
        status := strings.ToUpper(match[3])
        key := [3]string{board, motor, status}
        if codeSeen[key] {
            continue
        }
        codeSeen[key] = true
        idx := strings.Index(text, match[0])
        if idx == -1 {
            idx = 0
        }
        entries = append(entries, codeEntry{board: board, motor: motor, status: status, hex: fmt.Sprintf("%s %s %s", board, motor, status), pos: idx})
        codes = append(codes, key)
    }

    resultsMap, err := db.LookupMotorStatusBatch(codes, model)
    if err != nil {
        resultsMap = make(map[[3]string]*db.MotorStatus)
    }

    var unmatched []codeEntry
    for _, entry := range entries {
        key := [3]string{entry.board, entry.motor, entry.status}
        ms := resultsMap[key]
        originalText := extractLineContext(text, entry.pos)
        if ms != nil {
            result := buildMotorMatch(ms, entry.board, entry.motor, entry.status)
            result.RawHex = entry.hex
            result.OriginalText = originalText
            matches = append(matches, result)
        } else {
            unmatched = append(unmatched, codeEntry{board: entry.board, motor: entry.motor, status: entry.status, hex: entry.hex, originalText: originalText})
        }
    }

    for _, u := range unmatched {
        msg := fmt.Sprintf("未识别故障码 [%s]，板卡:%s 电机:%s 状态:%s，请补充电机状态表数据", u.hex, u.board, u.motor, u.status)
        matches = append(matches, MotorMatch{
            Type:         "motor_status_match",
            BoardCard:    u.board,
            MotorCode:    u.motor,
            StatusCode:   u.status,
            Diagnosis:    msg,
            Advice:       msg,
            Source:       "电机状态表(未匹配)",
            Keywords:     []string{u.board + " " + u.motor + " " + u.status},
            RawHex:       u.hex,
            Unmatched:    true,
            OriginalText: u.originalText,
        })
    }
    return matches
}

func buildMotorMatch(ms *db.MotorStatus, board, motor, status string) MotorMatch {
    diagnosis := ms.FullDescription
    if diagnosis == "" {
        diagnosis = ms.Description + "失败/异常"
    }
    var commandParts []string
    if ms.ActionType != "" {
        commandParts = append(commandParts, ms.ActionType)
    }
    if ms.TargetValue != "" {
        commandParts = append(commandParts, ms.TargetValue)
    }
    if ms.Sensor != "" {
        commandParts = append(commandParts, ms.Sensor)
    }
    command := strings.Join(commandParts, " | ")
    if command == "" {
        command = ms.Description
    }
    return MotorMatch{
        Type:            "motor_status_match",
        BoardCard:       ms.BoardCard,
        MotorCode:       ms.MotorCode,
        StatusCode:      ms.StatusCode,
        MotorName:       ms.MotorName,
        ActionType:      ms.ActionType,
        TargetValue:     ms.TargetValue,
        Sensor:          ms.Sensor,
        Description:     ms.Description,
        FullDescription: ms.FullDescription,
        Diagnosis:       diagnosis,
        Command:         command,
        Keywords:        []string{board + " " + motor + " " + status},
        Advice:          diagnosis,
        Source:          "电机状态表",
    }
}
