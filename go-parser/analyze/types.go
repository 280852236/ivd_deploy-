package analyze

type FileMeta struct {
	Name       string `json:"name"`
	RawName    string `json:"raw_name"`
	Size       int64  `json:"size"`
	IsCritical bool   `json:"is_critical"`
	FileType   string `json:"file_type"`
}

type FileResult struct {
	Name                string `json:"name"`
	Size                int64  `json:"size"`
	IsCritical          bool   `json:"is_critical"`
	FileType            string `json:"file_type"`
	IsAspirationFile    bool   `json:"is_aspiration_file"`
	HasFault            bool   `json:"has_fault"`
	HasAspirationMatch  bool   `json:"has_aspiration_match"`
	NotLoaded           bool   `json:"not_loaded"`
}

type DateGroup struct {
	Date  string      `json:"date"`
	Files []GroupFile `json:"files"`
}

type GroupFile struct {
	Name                string   `json:"name"`
	Size                int64    `json:"size"`
	IsCritical          bool     `json:"is_critical"`
	Types               []string `json:"types"`
	HasFault            bool     `json:"has_fault"`
	IsAspirationFile    bool     `json:"is_aspiration_file"`
	HasAspirationMatch  bool     `json:"has_aspiration_match"`
}

type Summary struct {
	Fault   int `json:"fault"`
	Sample  int `json:"sample"`
	Reagent int `json:"reagent"`
	Receive int `json:"receive"`
}

type AnalyzeResult struct {
	Files        map[string]*FileResult `json:"files"`
	FileMeta     []FileMeta             `json:"file_metadata"`
	DateGroups   []DateGroup            `json:"date_groups"`
	Summary      Summary                `json:"summary"`
	TotalFiles   int                    `json:"total_files"`
	TotalDates   int                    `json:"total_dates"`
	ZipProcessed int                    `json:"zip_processed"`
}
