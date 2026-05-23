//! Sample Rust module with deliberately questionable patterns.

use std::collections::HashMap;

pub struct User {
    pub name: String,
    pub email: String,
}

impl User {
    pub fn new(name: String, email: String) -> Self {
        User { name, email }
    }

    pub fn greet(&self) -> String {
        format!("Hello, {}!", self.name)
    }
}

pub trait Greeter {
    fn say_hi(&self) -> String;
}

pub fn unsafe_helper(ptr: *const u8) -> u8 {
    unsafe { *ptr }
}

pub fn unwrap_demo(opt: Option<i32>) -> i32 {
    opt.unwrap()
}
