package handlers

import (
	"encoding/json"
	"io"
	"net/http"
	"ivd_analyzer_go/analyze"
)

type SearchRequest struct {
	Content      string `json:"content"`
	Pattern      string `json:"pattern"`
	IsRegex      bool   `json:"is_regex"`
	CaseSensitive bool  `json:"case_sensitive"`
	WholeWord    bool   `json:"whole_word"`
}

func SearchHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req SearchRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 10*1024*1024)).Decode(&req); err != nil {
		http.Error(w, "Invalid request: "+err.Error(), http.StatusBadRequest)
		return
	}

	result := analyze.SearchText(req.Content, req.Pattern, req.IsRegex, req.CaseSensitive, req.WholeWord)

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	json.NewEncoder(w).Encode(result)
}
