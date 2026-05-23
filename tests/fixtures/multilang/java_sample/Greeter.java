import java.util.List;
import java.util.ArrayList;

public class Greeter {
    private String name;
    private List<String> messages;

    public Greeter(String name) {
        this.name = name;
        this.messages = new ArrayList<>();
    }

    public String hello() {
        return "Hello, " + name + "!";
    }

    public void addMessage(String msg) {
        messages.add(msg);
    }

    private static int counter = 0;

    public static int nextId() {
        return ++counter;
    }
}
