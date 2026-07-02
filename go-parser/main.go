package main

import (
    "log"
    "net/http"
    "ivd_analyzer_go/config"
    "ivd_analyzer_go/db"
    "ivd_analyzer_go/handlers"
)

func main() {
    cfg := config.Load()
    err := db.Init(cfg)
    if err != nil {
        log.Fatal("Failed to connect to database:", err)
    }
    defer db.DB.Close()

    http.HandleFunc("/parse", handlers.ParseHandler)
    log.Printf("Go parser listening on :%s", cfg.ServerPort)
    log.Fatal(http.ListenAndServe(":"+cfg.ServerPort, nil))
}
