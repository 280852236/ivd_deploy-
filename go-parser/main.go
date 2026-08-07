package main

import (
	"context"
	"log"
	"net/http"
	_ "net/http/pprof"
	"os"
	"os/signal"
	"runtime"
	"runtime/debug"
	"syscall"
	"time"
	"ivd_analyzer_go/config"
	"ivd_analyzer_go/db"
	"ivd_analyzer_go/handlers"
)

func main() {
	cfg := config.Load()

	runtime.GOMAXPROCS(runtime.NumCPU())
	debug.SetGCPercent(100)
	debug.SetMemoryLimit(512 * 1024 * 1024)

	err := db.Init(cfg)
	if err != nil {
		log.Fatal("Failed to connect to database:", err)
	}
	defer db.DB.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/parse", handlers.ParseHandler)
	mux.HandleFunc("/analyze", handlers.AnalyzeHandler)
	mux.HandleFunc("/search", handlers.SearchHandler)
	mux.HandleFunc("/health", healthHandler)

	if os.Getenv("GO_DEBUG") == "1" {
		go func() {
			log.Println("pprof on :6060")
			http.ListenAndServe("localhost:6060", nil)
		}()
	}

	srv := &http.Server{
		Addr:              ":" + cfg.ServerPort,
		Handler:           mux,
		ReadTimeout:       30 * time.Second,
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	go func() {
		log.Printf("Go parser listening on :%s (endpoints: /parse /analyze /search /health)", cfg.ServerPort)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal("Server error:", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
log.Println("Shutting down gracefully...")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal("Forced shutdown:", err)
	}
	log.Println("Server stopped")
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"ok"}`))
}
