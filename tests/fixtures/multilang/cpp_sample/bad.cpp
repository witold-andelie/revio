#include <cstdio>
#include <cstring>

int null_deref() {
    int* p = nullptr;
    return *p;  // null pointer dereference
}

int uninit_read() {
    int x;
    return x + 1;  // uninitialized variable read
}

void obvious_overflow() {
    char buf[5];
    strcpy(buf, "this is way too long for a 5-char buffer");  // buffer overflow
    printf("%s\n", buf);
}

int main() {
    null_deref();
    uninit_read();
    obvious_overflow();
    return 0;
}
