package handlers

import (
    "encoding/json"
    "net/http"
    "ivd_analyzer_go/matcher"
)

type ParseRequest struct {
    Text             string `json:"text"`
    Series           string `json:"series"`
    Model            string `json:"model"`
    SkipMotorStatus  bool   `json:"skip_motor_status"`
}

type ParseResponse struct {
    Results []matcher.AnalysisItem `json:"results"`
}

func ParseHandler(w http.ResponseWriter, r *http.Request) {
    if r.Method != http.MethodPost {
        http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
        return
    }
    var req ParseRequest
    if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
        http.Error(w, "Invalid request body", http.StatusBadRequest)
        return
    }
    results := matcher.AnalyzeText(req.Text, req.Series, req.Model, req.SkipMotorStatus)
    resp := ParseResponse{Results: results}
    w.Header().Set("Content-Type", "application/json")
    json.NewEncoder(w).Encode(resp)
}
