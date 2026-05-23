package main

import (
    "fmt"
    "strings"
)

type User struct {
    Name string
    Age  int
}

func NewUser(name string, age int) *User {
    return &User{Name: name, Age: age}
}

func (u *User) Greet() string {
    return fmt.Sprintf("Hello, %s (age %d)!", u.Name, u.Age)
}

func (u *User) NormalizedName() string {
    return strings.ToLower(u.Name)
}

func main() {
    u := NewUser("Alice", 30)
    fmt.Println(u.Greet())
}
