package com.example.demo;

interface Greeter {
    String greet(String name);
}

abstract class BaseGreeter {
    protected final String prefix;

    protected BaseGreeter(String prefix) {
        this.prefix = prefix;
    }

    protected String format(String name) {
        return prefix + name;
    }
}

final class ConsoleGreeter extends BaseGreeter implements Greeter {
    ConsoleGreeter(String prefix) {
        super(prefix);
    }

    @Override
    public String greet(String name) {
        return format(name);
    }
}

public final class App {
    private App() {
    }

    public static void main(String[] args) {
        Greeter greeter = new ConsoleGreeter("Hello, ");
        System.out.println(greeter.greet("SCIP"));
    }
}
