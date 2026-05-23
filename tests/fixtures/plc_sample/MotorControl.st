PROGRAM Main
VAR_INPUT
    Speed : REAL;
    Divisor : REAL;
    Pressure : REAL;
END_VAR
VAR_OUTPUT
    o_Motor : BOOL;
    o_Valve : BOOL;
END_VAR
VAR
    Result : REAL;
    MAX_SPEED : INT := 1500;
END_VAR

    (* Multiple violations packed in for testing *)

    // PLC-006: division without zero check
    Result := Speed / Divisor;

    // PLC-001: direct I/O address in program body
    %Q0.0 := o_Motor;

    // PLC-007 + PLC-009: output write without interlock or E-stop
    o_Motor := Speed > 100;
    o_Valve := Pressure > 50;

    // PLC-003: hardcoded magic number (large literal)
    IF Speed > 99999 THEN
        o_Motor := FALSE;
    END_IF;

    // PLC-011: floating-point equality
    IF Speed = 100.0 THEN
        o_Motor := TRUE;
    END_IF;

    // PLC-020: GOTO usage
    GOTO end_loop;
    end_loop:

END_PROGRAM
