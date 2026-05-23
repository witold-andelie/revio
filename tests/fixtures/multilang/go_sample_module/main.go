package main

import (
    "fmt"
    "os/exec"
)

func unusedFunction() {}

func runUnsafe(userInput string) {
    cmd := exec.Command("sh", "-c", userInput)  // command injection
    cmd.Run()
}

func main() {
    var x int
    fmt.Println("hello", x)  // x is unused-but-assigned-default
}
