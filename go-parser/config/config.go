package config

import (
    "os"
)

type Config struct {
    DBHost     string
    DBPort     string
    DBUser     string
    DBPassword string
    DBName     string
    ServerPort string
}

func Load() *Config {
    return &Config{
        DBHost:     getEnv("DB_HOST", "172.22.64.1"),
        DBPort:     getEnv("DB_PORT", "5432"),
        DBUser:     getEnv("DB_USER", "ivd_user"),
        DBPassword: getEnv("DB_PASSWORD", "ivd_pass"),
        DBName:     getEnv("DB_NAME", "ivd_fault_db"),
        ServerPort: getEnv("SERVER_PORT", "8082"),
    }
}

func getEnv(key, fallback string) string {
    if val, ok := os.LookupEnv(key); ok {
        return val
    }
    return fallback
}
