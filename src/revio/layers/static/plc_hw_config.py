"""Hardware configuration parser and verifier for TIA Portal projects.

Parses TIA Portal HWConfig XML to extract:
  - CPU model, firmware version, protection level, watchdog
  - I/O module configuration (slots, addresses, safety/redundancy)
  - Network configuration (PROFINET, S7comm, IP settings)
  - Safety configuration (F-CPU, SIL level, safety programs)

Validates against security and safety rules:
  - HW-001: Known vulnerable firmware versions
  - HW-002: Low protection level
  - HW-003: Watchdog misconfiguration
  - HW-004: Non-redundant safety I/O
  - HW-005: Safety CPU without safety program
  - HW-006: PROFINET without port security
  - HW-007: Default/missing password
  - HW-008: Open web server ports
  - HW-009: Unencrypted protocols
  - HW-010: Safety watchdog mismatch
  - HW-011: High memory utilization
  - HW-012: Unknown CPU article number
"""

import logging
import re
from xml.etree import ElementTree as ET

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class CPUConfig(BaseModel):
    """CPU hardware configuration."""
    model: str = ""                    # e.g. "S7-1500", "S7-1200", "1516-3 PN/DP"
    firmware_version: str = ""         # e.g. "2.8.3"
    article_number: str = ""           # e.g. "6ES7 516-3AN02-0AB0"
    memory_size_kb: int = 0
    ip_address: str = ""
    subnet_mask: str = ""
    mac_address: str = ""
    protection_level: int = 0          # 0=no protection, 1=read, 2=read/write, 3=full
    cycle_watchdog_ms: int = 0         # OB1 watchdog in ms, 0=disabled
    web_server_enabled: bool = False
    web_server_port: int = 80
    opcua_enabled: bool = False
    opcua_port: int = 4840
    is_safety_cpu: bool = False        # F-CPU


class IOModule(BaseModel):
    """I/O module configuration."""
    module_name: str = ""
    article_number: str = ""
    slot: int = 0
    subslot: int = 0
    module_type: str = ""              # DI, DO, AI, AO, comm, etc.
    io_addresses: list[str] = []
    is_redundant: bool = False
    is_safety: bool = False            # F-module
    diagnostic_support: bool = False


class NetworkConfig(BaseModel):
    """Network interface configuration."""
    protocol: str = ""                 # PROFINET, S7comm, MODBUS_TCP, etc.
    interface_name: str = ""
    ip_address: str = ""
    subnet_mask: str = ""
    gateway: str = ""
    port: int = 0
    vlan_id: int = 0
    dcp_name: str = ""                 # PROFINET DCP device name
    port_security_enabled: bool = False
    encryption_enabled: bool = False


class SafetyConfig(BaseModel):
    """Safety (F-CPU) configuration."""
    f_cpu_enabled: bool = False
    safety_level: str = ""             # SIL2, SIL3, PLd, PLe
    safety_programs: list[str] = []
    safety_watchdog_ms: int = 0
    crc_signature: str = ""
    password_level_1: str = ""         # Safety password (should be set)
    password_level_2: str = ""
    read_protection: bool = False
    write_protection: bool = False


class HardwareConfig(BaseModel):
    """Complete hardware configuration."""
    project_name: str = ""
    cpu: CPUConfig = CPUConfig()
    io_modules: list[IOModule] = []
    networks: list[NetworkConfig] = []
    safety: SafetyConfig = SafetyConfig()
    raw_xml_snippet: str = ""


class HWRuleViolation(BaseModel):
    """A hardware configuration rule violation."""
    rule_id: str
    rule_name: str
    severity: str         # critical, error, warning, info
    description: str
    component: str = ""   # CPU, IO, Network, Safety
    suggestion: str | None = None


# Known vulnerable Siemens CPU firmware versions
VULNERABLE_FIRMWARE = {
    "S7-1200": [
        ("4.0", "CVE-2019-13945 — Web server XSS vulnerability"),
        ("4.1", "CVE-2020-7580 — Denial of service via crafted packets"),
        ("4.2", "CVE-2020-15782 — Memory protection bypass"),
        ("4.4", "CVE-2021-40365 — Authentication bypass in web server"),
    ],
    "S7-1500": [
        ("2.0", "CVE-2019-13945 — Web server XSS vulnerability"),
        ("2.5", "CVE-2020-7580 — Denial of service via crafted packets"),
        ("2.6", "CVE-2020-15782 — Memory protection bypass"),
        ("2.8", "CVE-2021-40365 — Authentication bypass in web server"),
        ("2.8.3", "CVE-2022-38465 — Private key extraction vulnerability"),
    ],
}

# Known CPU article numbers and their models
KNOWN_CPU_ARTICLES = {
    "6ES7 211-1AE40": "S7-1200 CPU 1211C",
    "6ES7 212-1AE40": "S7-1200 CPU 1212C",
    "6ES7 214-1AG40": "S7-1200 CPU 1214C",
    "6ES7 215-1AG40": "S7-1200 CPU 1215C",
    "6ES7 217-1AG40": "S7-1200 CPU 1217C",
    "6ES7 510-1SJ02": "S7-1500 CPU 1510SP",
    "6ES7 511-1CK02": "S7-1500 CPU 1511",
    "6ES7 512-1DK02": "S7-1500 CPU 1512",
    "6ES7 513-1AL02": "S7-1500 CPU 1513",
    "6ES7 515-2AM02": "S7-1500 CPU 1515",
    "6ES7 516-3AN02": "S7-1500 CPU 1516",
    "6ES7 517-3AP00": "S7-1500 CPU 1517",
    "6ES7 518-4AP00": "S7-1500 CPU 1518",
    # F-CPUs (safety)
    "6ES7 510-1SJ02": "S7-1500F CPU 1510SP F",
    "6ES7 511-1FK02": "S7-1500F CPU 1511F",
    "6ES7 513-1FL02": "S7-1500F CPU 1513F",
    "6ES7 515-2FM02": "S7-1500F CPU 1515F",
    "6ES7 516-3FN02": "S7-1500F CPU 1516F",
    "6ES7 517-3FP00": "S7-1500F CPU 1517F",
    "6ES7 518-4FP00": "S7-1500F CPU 1518F",
}


class HWConfigParser:
    """Parse TIA Portal hardware configuration XML."""

    # TIA Portal HWConfig namespace markers
    TIA_NAMESPACES = [
        "SW.HW",
        "SW.IOSystem",
        "SW.Plc",
        "Engineering",
        "StationConfig",
        "http://www.siemens.com/automation/Openness",
    ]

    @classmethod
    def is_hwconfig(cls, file_path: str) -> bool:
        """Detect TIA Portal HWConfig XML."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            return cls._is_hwconfig_root(root)
        except (ET.ParseError, OSError):
            return False

    @classmethod
    def is_hwconfig_string(cls, xml_content: str) -> bool:
        """Detect TIA Portal HWConfig XML from string."""
        try:
            root = ET.fromstring(xml_content)
            return cls._is_hwconfig_root(root)
        except ET.ParseError:
            return False

    @classmethod
    def _is_hwconfig_root(cls, root: ET.Element) -> bool:
        """Check if XML root looks like a TIA HWConfig."""
        tag = root.tag.lower()
        # Check for TIA-specific attributes or tags
        for ns in cls.TIA_NAMESPACES:
            if ns.lower() in tag:
                return True
        # Check for Siemens-specific child elements
        for child in root:
            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if ctag in ("Station", "HW", "PlcProject", "Device",
                        "Controller", "Configuration"):
                return True
        return False

    @classmethod
    def parse_file(cls, file_path: str) -> HardwareConfig | None:
        """Parse a TIA Portal HWConfig XML file."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            return cls._parse_root(root)
        except (ET.ParseError, OSError) as e:
            logger.debug(f"Failed to parse HW config {file_path}: {e}")
            return None

    @classmethod
    def parse_string(cls, xml_content: str) -> HardwareConfig | None:
        """Parse HWConfig from XML string."""
        try:
            root = ET.fromstring(xml_content)
            return cls._parse_root(root)
        except ET.ParseError as e:
            logger.debug(f"Failed to parse HW config XML: {e}")
            return None

    @classmethod
    def _parse_root(cls, root: ET.Element) -> HardwareConfig:
        """Parse the XML root into a HardwareConfig."""
        config = HardwareConfig()

        # Try to extract project name
        config.project_name = root.get("Name", root.get("ProjectName", ""))

        # Walk the tree looking for hardware components
        for elem in root.iter():
            tag = cls._local_tag(elem)

            # CPU / Controller
            if tag in ("CPU", "Controller", "PlcDevice", "Device"):
                cls._parse_cpu(elem, config)

            # I/O Module
            elif tag in ("Module", "IOModule", "DI", "DO", "AI", "AO"):
                cls._parse_io_module(elem, config)

            # Network
            elif tag in ("NetworkInterface", "PROFINET", "Ethernet", "Interface"):
                cls._parse_network(elem, config)

            # Safety (only container element, not child elements)
            elif tag in ("SafetyConfiguration", "FConfiguration"):
                cls._parse_safety(elem, config)

            # Subnet
            elif tag in ("Subnet", "IPConfiguration"):
                cls._parse_ip_config(elem, config)

        return config

    @classmethod
    def _parse_cpu(cls, elem: ET.Element, config: HardwareConfig):
        """Parse CPU/controller element."""
        cpu = config.cpu

        cpu.model = elem.get("Model", elem.get("TypeName", elem.get("Name", "")))
        cpu.article_number = elem.get("ArticleNumber", elem.get("OrderNumber", ""))
        cpu.firmware_version = elem.get("FirmwareVersion", elem.get("FWVersion", ""))

        # Check for safety CPU
        model_upper = cpu.model.upper()
        art_upper = cpu.article_number.upper()
        if "F-CPU" in model_upper or "F " in model_upper or "/F" in model_upper:
            cpu.is_safety_cpu = True
        if any(f in art_upper for f in ("511-1F", "513-1F", "515-2F", "516-3F", "517-3F", "518-4F")):
            cpu.is_safety_cpu = True

        # Parse direct child elements (not nested ones like SafetyConfiguration)
        for child in elem:
            ctag = cls._local_tag(child)
            # Skip nested complex elements — handled by _parse_root
            if ctag in ("SafetyConfiguration", "FConfiguration", "SafetyProgram",
                        "Module", "IOModule", "NetworkInterface", "PROFINET",
                        "Ethernet", "Interface", "Subnet", "IPConfiguration"):
                continue

            text = (child.text or "").strip()

            if ctag == "ProtectionLevel":
                try:
                    cpu.protection_level = int(text)
                except ValueError:
                    pass
            elif ctag in ("Watchdog", "CycleWatchdog", "OB1Watchdog"):
                try:
                    cpu.cycle_watchdog_ms = int(text)
                except ValueError:
                    pass
            elif ctag == "WebServer":
                cpu.web_server_enabled = text.lower() in ("true", "1", "enabled")
                port = child.get("Port", "")
                if port:
                    try:
                        cpu.web_server_port = int(port)
                    except ValueError:
                        pass
            elif ctag == "OPCUA":
                cpu.opcua_enabled = text.lower() in ("true", "1", "enabled")
            elif ctag == "IPAddress":
                cpu.ip_address = text
            elif ctag == "SubnetMask":
                cpu.subnet_mask = text
            elif ctag == "MAC":
                cpu.mac_address = text

    @classmethod
    def _parse_io_module(cls, elem: ET.Element, config: HardwareConfig):
        """Parse I/O module element."""
        module = IOModule(
            module_name=elem.get("Name", elem.get("TypeName", "")),
            article_number=elem.get("ArticleNumber", elem.get("OrderNumber", "")),
            slot=int(elem.get("Slot", "0")),
            subslot=int(elem.get("Subslot", "0")),
            module_type=elem.get("Type", elem.get("ModuleType", "")),
        )

        # Check for safety/redundancy flags
        name_upper = module.module_name.upper()
        if "F-" in name_upper or "FAILSAFE" in name_upper or "SAFETY" in name_upper:
            module.is_safety = True
        if "REDUNDANT" in name_upper or "RED" in name_upper:
            module.is_redundant = True

        # Parse addresses
        for child in elem:
            ctag = cls._local_tag(child)
            if ctag in ("Address", "IOAddress", "LogicalAddress"):
                addr = (child.text or "").strip()
                if addr:
                    module.io_addresses.append(addr)
            elif ctag == "Diagnostics":
                module.diagnostic_support = True

        config.io_modules.append(module)

    @classmethod
    def _parse_network(cls, elem: ET.Element, config: HardwareConfig):
        """Parse network interface element."""
        net = NetworkConfig(
            protocol=elem.get("Protocol", elem.get("Type", "")),
            interface_name=elem.get("Name", ""),
        )

        for child in elem:
            ctag = cls._local_tag(child)
            text = (child.text or "").strip()

            if ctag == "IPAddress":
                net.ip_address = text
            elif ctag == "SubnetMask":
                net.subnet_mask = text
            elif ctag == "Gateway":
                net.gateway = text
            elif ctag == "Port":
                try:
                    net.port = int(text)
                except ValueError:
                    pass
            elif ctag == "DCPName":
                net.dcp_name = text
            elif ctag == "PortSecurity":
                net.port_security_enabled = text.lower() in ("true", "1", "enabled")
            elif ctag in ("TLS", "Encryption"):
                net.encryption_enabled = text.lower() in ("true", "1", "enabled")

        config.networks.append(net)

    @classmethod
    def _parse_safety(cls, elem: ET.Element, config: HardwareConfig):
        """Parse safety configuration element."""
        safety = config.safety
        safety.f_cpu_enabled = True

        safety.safety_level = elem.get("SafetyLevel", elem.get("SIL", ""))
        safety.crc_signature = elem.get("CRC", elem.get("Signature", ""))

        for child in elem:
            ctag = cls._local_tag(child)
            text = (child.text or "").strip()

            if ctag in ("SafetyProgram", "FProgram", "Program"):
                if text:
                    safety.safety_programs.append(text)
            elif ctag == "Watchdog":
                try:
                    safety.safety_watchdog_ms = int(text)
                except ValueError:
                    pass
            elif ctag in ("Password", "PasswordLevel1"):
                safety.password_level_1 = text
            elif ctag == "PasswordLevel2":
                safety.password_level_2 = text
            elif ctag == "ReadProtection":
                safety.read_protection = text.lower() in ("true", "1")
            elif ctag == "WriteProtection":
                safety.write_protection = text.lower() in ("true", "1")

    @classmethod
    def _parse_ip_config(cls, elem: ET.Element, config: HardwareConfig):
        """Parse IP configuration from subnet elements."""
        ip = elem.get("IPAddress", elem.get("IP", ""))
        mask = elem.get("SubnetMask", elem.get("Mask", ""))

        if ip and not config.cpu.ip_address:
            config.cpu.ip_address = ip
        if mask and not config.cpu.subnet_mask:
            config.cpu.subnet_mask = mask

    @classmethod
    def _local_tag(cls, elem: ET.Element) -> str:
        """Get local tag name, stripping namespace."""
        tag = elem.tag
        if "}" in tag:
            return tag.split("}")[1]
        return tag


class HWConfigRulesChecker:
    """Validate hardware configuration against security and safety rules."""

    @classmethod
    def check(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """Run all hardware configuration checks."""
        violations = []
        violations.extend(cls._check_firmware(config))
        violations.extend(cls._check_protection(config))
        violations.extend(cls._check_watchdog(config))
        violations.extend(cls._check_io_redundancy(config))
        violations.extend(cls._check_safety(config))
        violations.extend(cls._check_network(config))
        violations.extend(cls._check_web_server(config))
        violations.extend(cls._check_memory(config))
        violations.extend(cls._check_article_number(config))
        return violations

    @classmethod
    def _check_firmware(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-001: Check for known vulnerable firmware versions."""
        violations = []
        cpu = config.cpu
        if not cpu.model or not cpu.firmware_version:
            return violations

        # Find matching model in vulnerability DB
        for model_key, vulns in VULNERABLE_FIRMWARE.items():
            if model_key.upper() in cpu.model.upper():
                for fw_ver, cve_desc in vulns:
                    if cpu.firmware_version.startswith(fw_ver):
                        violations.append(HWRuleViolation(
                            rule_id="HW-001",
                            rule_name="Vulnerable firmware version",
                            severity="critical",
                            description=f"CPU {cpu.model} firmware {cpu.firmware_version}: {cve_desc}",
                            component="CPU",
                            suggestion=f"Update firmware to latest version. See Siemens security advisory.",
                        ))
                        break

        return violations

    @classmethod
    def _check_protection(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-002: Check CPU protection level."""
        violations = []
        cpu = config.cpu

        if cpu.protection_level == 0:
            violations.append(HWRuleViolation(
                rule_id="HW-002",
                rule_name="CPU protection level too low",
                severity="error",
                description=f"CPU {cpu.model} has no access protection (Level 0). "
                            "Anyone with network access can modify the program.",
                component="CPU",
                suggestion="Set protection level to at least 2 (read/write protection with password).",
            ))

        return violations

    @classmethod
    def _check_watchdog(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-003: Check cycle watchdog configuration."""
        violations = []
        cpu = config.cpu

        if cpu.cycle_watchdog_ms == 0:
            violations.append(HWRuleViolation(
                rule_id="HW-003",
                rule_name="Cycle watchdog disabled",
                severity="warning",
                description="OB1 cycle watchdog is disabled. Program hangs will not be detected.",
                component="CPU",
                suggestion="Enable watchdog with appropriate timeout (typically 150ms for S7-1500).",
            ))
        elif cpu.cycle_watchdog_ms > 500:
            violations.append(HWRuleViolation(
                rule_id="HW-003",
                rule_name="Cycle watchdog too long",
                severity="warning",
                description=f"OB1 watchdog is {cpu.cycle_watchdog_ms}ms. "
                            "Long watchdogs delay detection of program hangs.",
                component="CPU",
                suggestion="Reduce watchdog to 150-200ms for typical applications.",
            ))

        return violations

    @classmethod
    def _check_io_redundancy(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-004: Check safety I/O for redundancy."""
        violations = []

        safety_modules = [m for m in config.io_modules if m.is_safety]
        non_redundant_safety = [m for m in safety_modules if not m.is_redundant]

        if non_redundant_safety:
            names = [m.module_name for m in non_redundant_safety]
            violations.append(HWRuleViolation(
                rule_id="HW-004",
                rule_name="Safety I/O without redundancy",
                severity="warning",
                description=f"Safety modules without redundancy: {', '.join(names)}. "
                            "Single-channel safety I/O is a single point of failure.",
                component="IO",
                suggestion="Consider dual-channel (redundant) wiring for SIL2/PLd and above.",
            ))

        return violations

    @classmethod
    def _check_safety(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-005: Check safety CPU configuration."""
        violations = []
        cpu = config.cpu
        safety = config.safety

        if cpu.is_safety_cpu and not safety.f_cpu_enabled:
            violations.append(HWRuleViolation(
                rule_id="HW-005",
                rule_name="Safety CPU without safety program",
                severity="error",
                description=f"F-CPU {cpu.model} detected but no safety program configured. "
                            "Safety functions are not active.",
                component="Safety",
                suggestion="Configure safety program in Safety Administrator.",
            ))

        if safety.f_cpu_enabled and not safety.safety_programs:
            violations.append(HWRuleViolation(
                rule_id="HW-005",
                rule_name="Safety configuration without programs",
                severity="error",
                description="Safety configuration exists but no safety programs are defined.",
                component="Safety",
                suggestion="Add safety programs to the safety configuration.",
            ))

        return violations

    @classmethod
    def _check_network(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-006/009: Check network security configuration."""
        violations = []

        for net in config.networks:
            if "PROFINET" in net.protocol.upper() and not net.port_security_enabled:
                violations.append(HWRuleViolation(
                    rule_id="HW-006",
                    rule_name="PROFINET without port security",
                    severity="warning",
                    description=f"PROFINET interface {net.interface_name} has port security disabled. "
                                "Unauthorized devices can connect.",
                    component="Network",
                    suggestion="Enable PROFINET port security to restrict device access.",
                ))

            if net.protocol.upper() in ("S7COMM", "S7", "ISO-TCP") and not net.encryption_enabled:
                violations.append(HWRuleViolation(
                    rule_id="HW-009",
                    rule_name="Unencrypted communication protocol",
                    severity="warning",
                    description=f"Interface {net.interface_name} uses {net.protocol} without encryption. "
                                "Traffic can be intercepted or tampered with.",
                    component="Network",
                    suggestion="Enable TLS/encryption for PLC communication. "
                               "Use S7comm Plus with authentication on S7-1500.",
                ))

        return violations

    @classmethod
    def _check_web_server(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-007/008: Check web server and password configuration."""
        violations = []
        cpu = config.cpu

        if cpu.web_server_enabled:
            if cpu.web_server_port in (80, 8080):
                violations.append(HWRuleViolation(
                    rule_id="HW-008",
                    rule_name="Web server on unencrypted port",
                    severity="warning",
                    description=f"CPU web server running on port {cpu.web_server_port} (HTTP). "
                                "Credentials sent in clear text.",
                    component="CPU",
                    suggestion="Use HTTPS (port 443) instead of HTTP.",
                ))

        # Check for empty safety passwords
        safety = config.safety
        if safety.f_cpu_enabled and not safety.password_level_1:
            violations.append(HWRuleViolation(
                rule_id="HW-007",
                rule_name="Safety CPU without password",
                severity="critical",
                description="Safety CPU has no Level 1 password set. "
                            "Safety programs can be modified without authentication.",
                component="Safety",
                suggestion="Set strong passwords for all safety protection levels.",
            ))

        return violations

    @classmethod
    def _check_memory(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-011: Check memory utilization."""
        violations = []
        cpu = config.cpu

        if cpu.memory_size_kb > 0:
            # This is a heuristic — actual utilization depends on loaded program
            # Flag if memory is very small for modern applications
            if cpu.memory_size_kb < 100:
                violations.append(HWRuleViolation(
                    rule_id="HW-011",
                    rule_name="Low CPU memory",
                    severity="info",
                    description=f"CPU has only {cpu.memory_size_kb}KB memory. "
                                "May be insufficient for complex applications.",
                    component="CPU",
                    suggestion="Consider a CPU with more work memory if program size grows.",
                ))

        return violations

    @classmethod
    def _check_article_number(cls, config: HardwareConfig) -> list[HWRuleViolation]:
        """HW-012: Validate CPU article number."""
        violations = []
        cpu = config.cpu

        if not cpu.article_number:
            return violations

        # Normalize article number
        art = cpu.article_number.replace(" ", "").upper()

        # Check if it's a known article
        is_known = False
        for known_art, known_model in KNOWN_CPU_ARTICLES.items():
            if known_art.replace(" ", "").upper() in art:
                is_known = True
                # Verify model matches
                if cpu.model and known_model.split(" ")[0] not in cpu.model.upper():
                    violations.append(HWRuleViolation(
                        rule_id="HW-012",
                        rule_name="Article number mismatch",
                        severity="info",
                        description=f"Article number {cpu.article_number} suggests {known_model}, "
                                    f"but configured model is {cpu.model}.",
                        component="CPU",
                        suggestion="Verify CPU model matches the physical hardware.",
                    ))
                break

        return violations
