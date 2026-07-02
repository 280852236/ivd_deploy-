package matcher

import "strings"

type AspirationResult struct {
    Matched    bool     `json:"matched"`
    Conditions []string `json:"conditions"`
    Type       string   `json:"type"`
}

func CheckAspirationAnomaly(line string) AspirationResult {
    var conditions []string
    matched := false
    fileType := "none"

    if strings.Contains(line, "样本") && (strings.Contains(line, "空吸") || strings.Contains(line, "取样本") || strings.Contains(line, "取稀释样本")) {
        fileType = "sample"
    } else if strings.Contains(line, "试剂") && (strings.Contains(line, "空吸") || strings.Contains(line, "试剂针")) {
        fileType = "reagent"
    }

    if fileType == "none" {
        return AspirationResult{Matched: false, Conditions: []string{}, Type: "none"}
    }

    if strings.Contains(line, "执行过重测：True") || strings.Contains(line, "执行过重测: True") {
        conditions = append(conditions, "执行过重测")
        matched = true
    }
    if strings.Contains(line, "余量不足：True") || strings.Contains(line, "余量不足: True") {
        conditions = append(conditions, "余量不足")
        matched = true
    }
    if strings.Contains(line, "电路异常：True") || strings.Contains(line, "电路异常: True") {
        conditions = append(conditions, "电路异常")
        matched = true
    }
    if strings.Contains(line, "脱离液面失败：True") || strings.Contains(line, "脱离液面失败: True") {
        conditions = append(conditions, "脱离液面失败")
        matched = true
    }
    if strings.Contains(line, "空吸：True") || strings.Contains(line, "空吸: True") {
        conditions = append(conditions, "空吸")
        matched = true
    }
    if strings.Contains(line, "重测3次失败：True") || strings.Contains(line, "重测3次失败: True") {
        conditions = append(conditions, "重测3次失败")
        matched = true
    }
    if strings.Contains(line, "液位探测有效：False") || strings.Contains(line, "液位探测有效: False") {
        conditions = append(conditions, "液位探测无效")
        matched = true
    }
    return AspirationResult{Matched: matched, Conditions: conditions, Type: fileType}
}
