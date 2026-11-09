# ENPC — Epson NetConfig Protocol

Field catalog and protocol notes, reverse-engineered from **EpsonNet Config V4**
(macOS build, `/Applications/Epson Software/EpsonNet/EpsonNet Config V4/EpsonNet Config.app`,
Mach-O universal binary, ~11 MB). The catalog cross-references what shows up in this
project's own firmware (see [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md), [NETWORK_SERVICES.md](NETWORK_SERVICES.md),
[ARCHITECTURE.md](ARCHITECTURE.md)).

- Full machine-readable catalog: [enpc_schema/CATALOG.tsv](enpc_schema/CATALOG.tsv) (1,651 rows)
- Per-item summary: [enpc_schema/ITEMS.tsv](enpc_schema/ITEMS.tsv) (154 rows)
- Decoded template files: [enpc_schema/](enpc_schema/) (1,570 files)
- Builder: [build_enpc_catalog.py](build_enpc_catalog.py) + wrapper [build_enpc_catalog.sh](build_enpc_catalog.sh) — re-runs the whole pipeline against the installed EpsonNet Config app; `enpc_schema/` is `.gitignore`d so the output is regenerated locally.

## 1. What ENPC is

ENPC is Epson's proprietary device-management **RPC over an operation set called
`CENPCPacket`**. It is what tools like *EpsonNet Config*, *EpsonNet Setup*, *Epson
Device Admin*, the Windows "Add Epson Printer" wizard, the mobile setup flows, and
some ink-reset / CISS utilities speak to configure and discover printers.

- **Namespaces** (baked into every template file):
  - `http://www.epson.com/EpsonNet/DynamicUI/1-0` — the DUI schema
  - `http://www.epson.com/EpsonNet/PackageConfig/1-0` — package-config schema
  - `http://www.epson.com/EpsonNet/ENCMWrapper/1-0` — dispatcher wrapper
  - `http://www.epson.com/EpsonNet/Common-Definition/1-0` — shared types
- **Not TLS-protected**. Authentication for admin operations is **MD5**
  challenge-response (class `CENMD5`, method `execLoginENPC`).
- Every operation has a **twin over SOAP** (`execGetDetailSOAP`, `execLoginSOAP`,
  `execFactResetSOAP`, …) — on devices that expose it, the same operation set is
  reachable via SOAP over HTTP. **Verified present on the XP-442** — see
  "WSD SOAP config" in §2.

## 2. Transports

`ENPC` is the packet format, not the transport. Classes `CENMDENPC`, `CENMDBSUSB`,
and `CENMDProbe` sit under one dispatcher. Observed transports:

| Wire name | Address | Purpose |
|---|---|---|
| `ENPC(Subnet Broadcast UDP)` | UDP **3289** (broadcast) | Discovery — a subnet-directed probe. Binary `CENPCPacket` framing (not yet reversed). |
| `ENPC(Subnet Unicast UDP)` | UDP **3289** (unicast) | Post-discovery config to a specific IP |
| `ENPC(Directed Broadcast UDP)` | UDP 3289 to one of 5 configured `RebootPolling.ENPC_for_UDP_Directed_1..5` slots | Cross-VLAN probing |
| `ENPC(BSUSB)` | Bidirectional-Serial USB (`CENBSUSB`, `CENMDBSUSB`) | Same packet format over USB, no network |
| `ENPC(BSCOM)` | Bidirectional-Serial COM (RS-232, `Probe.ENPC_for_BSCOM`) | Legacy serial, still supported |
| **WSD SOAP config** | **HTTP POST `/Epson_WS_Config` on TCP 80 / 443** | **Same operation set as ENPC (see §3), but SOAP 1.2 + WS-Addressing over HTTP — no binary framing to reverse.** |

Peer protocols on the same dispatcher: **SNMP** (`CENSNMPPacket`) and **SLP**
(`CENSLPPacket`). If a device speaks ENPC on 3289, it almost certainly answers
SLP probes too.

### WSD SOAP config — verified live on the XP-442

The XP-442 exposes a single SOAP endpoint at **`http://scanner.lan/Epson_WS_Config`**
served by a distinct daemon that reports `Server: wsd/1.0` (separate from the
public HTML UI's `EPSON_Linux UPnP/1.0` and from the WebConfigEx admin's
`WebConfigEx/1.0`).

- **Protocol**: SOAP 1.2 (`application/soap+xml`) with **WS-Addressing 2004/08**
  headers. The operation is selected by `<wsa:Action>` inside the envelope, not
  by URL path.
- **Action URIs** (all under `http://schemas.epson.net/ws/2008/07/config`):

  | Verb | Request Action URI | Response Action URI |
  |---|---|---|
  | GetData | `…/config/GetData` | `…/config/GetDataResponse` |
  | SetData | `…/config/SetData` | `…/config/SetDataResponse` |
  | GetFile | `…/config/GetFile` (blobs, e.g. certs) | `…/config/GetFileResponse` |
  | SetFile | `…/config/SetFile` | `…/config/SetFileResponse` |

  > **Correction from earlier notes:** these are **wsa:Action URIs**, not HTTP
  > URL paths. All four operations POST to the same `/Epson_WS_Config` URL;
  > dispatch is by the Action header.
- **Auth**: no 401 challenge on GetData without credentials. Whether SetData/
  SetFile need auth (via WebConfigEx session cookie or WS-Security) is not yet
  tested.

Live probe results against `scanner.lan` (firmware `20.24.RO05IA.FY16`):

| Request | Response | Reading |
|---|---|---|
| Malformed XML | **HTTP 400** | Real XML parser wired to the endpoint |
| Valid envelope, unknown `wsa:Action` | **connection hangs → timeout** | URL routes, but no fallback for unknown actions |
| Valid envelope, `wsa:Action=…/config/GetData`, empty body | **HTTP 202 Accepted** (no body) | Dispatched; response delivered async, not inline |
| Rapid repeats | **HTTP 503** | WSD daemon is fragile under load |

The 202-with-empty-body is standard WSD behaviour: WSD is designed to POST the
response to a callback URL named by `wsa:ReplyTo`. Setting `ReplyTo` to the
WS-A anonymous URI (which normally signals "reply on this connection") did
**not** produce an inline response on this device — either GetData needs input
parameters and returns silently on empty, or this build only supports true
async callback delivery, requiring a client-side HTTP listener.

## 3. Operation set

Direct methods on `CENCMNIFInfo` — these are the RPC verbs.

| ENPC method | SOAP twin | Purpose |
|---|---|---|
| `execGetListInfoENPC` | `execGetListInfoSOAP` | Probe reply — device reports the template set it supports |
| `execGetDetailENPC` | `execGetDetailSOAP` | Read a batch of field values |
| `execGetStatusListInfoENPC` | `execGetStatusListInfoSOAP` | Read the volatile status subset |
| `execRefreshDataENPC` | `execRefreshDataSOAP` | Poll for changed fields |
| `execSetDetailENPC` | `execSetDetailSOAP` | Write field values |
| `execBatchSettingGetDetailENPC` | `execBatchSettingGetDetailSOAP` | Bulk read (Batch Settings wizard) |
| `execBatchSettingSetDetailENPC` | `execBatchSettingSetDetailSOAP` | Bulk write |
| `execExecCommandENPC` | — | Invoke a named side-effect (reboot, wake, etc.) |
| `execFactResetENPC(int nCommand)` | `execFactResetSOAP` | Factory reset. Uses `FactResetPacket`; requires `FactResetUnlock` first |
| `execLoginENPC` | `execLoginSOAP` | Authenticate as admin (MD5 challenge-response) |
| `execChangePasswordENPC` | `execChangePasswordSOAP` | Rotate admin password |
| `execRebootPollingENPC` | `execRebootPollingSOAP` | Poll for the device to come back after a reboot |
| `execRecoveryFromPowerSavingENPC` | — | Wake from sleep before further ops |

**Session lock is cooperative**: `ENPC.LockMode` / `ENPC.LockTimeout` fields wrap
any `SetDetail` sequence. Matches the firmware's `COMIF_LEVT_LOCKFLASH` /
`UNLOCKFLASH` events ([ARCHITECTURE.md](ARCHITECTURE.md)).

**Error taxonomy** (all as `EEN*Exception` in the client — the wire protocol
carries these as error codes):
`Communication`, `Timeout`, `Login`, `Lock`, `Unlock`, `InvalidParam`,
`InvalidCall`, `DefineFormat`, `ErrorResult`, `Unsupported`, `IO`, `Open`,
`ScriptCompile`, `Script`, `Xml`, `FileFormatNotSupport`, `NotExist`,
`NullObject`, `MemoryAlloc`, `StartThread`, `StopFunction`, `Terminate`,
`Unexpected`.

## 4. Field-ID model

Every configurable and readable value has a **fully-qualified name** in the
form:

```
ENPC.<CC>_<IIII>[_<SS>].<FieldName>[.<sub>...]
```

- `<CC>` — 2-hex-digit **category** (see §5)
- `<IIII>` — 4-hex-digit **item ID** within the category
- `_<SS>` (optional) — 2-hex-digit **sub-record selector**, used when an item
  is a variant record (e.g. `ENPC.00_0005_07.HardwareType`)
- `<FieldName>` — logical field name (`EnableIP`, `SSID`, `ProxyPassword`, …)
- `.<sub>` — for repeated structures, a numeric array index and inner field
  (`ENPC.00_0057.ESSID.2.SecurityType` = SSID slot 2's security-type field)

On the wire the pair `(CC, IIII)` is the numeric TLV key; the field names above
are client-side aliases fetched from `DUIInfo_<template>.dat`.

**Relationship to PNRSC (important — read before assuming equivalence):** the
ENPC namespace has the **same shape** as PNRSC (category + numeric item ID)
and clearly derives from it, but the two spaces are **not literally
identical**. ENPC categories in this build max out at `05`; PNRSC subjects
extend well past that (telnet lives at PNRSC subject `0x37`, item `0xc2` —
[PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md#L111)). Every ENPC field must land somewhere in PNRSC
via a remap table inside `PNENPCServiceStart`, but many PNRSC entries have
**no ENPC counterpart at all** (telnet, low-level flash control, and other
service-only items). In effect ENPC is a **curated, remapped subset** of
PNRSC — the subset that Epson intended to expose to network configuration
clients. That subset is your attack-surface enumeration; PNRSC entries missing
from it are only reachable via COMIF, USB, or the panel.

## 5. Categories

Six top-level categories in this build. Item counts are from the union across
all shipped templates:

| Cat | Purpose | Items |
|:---:|---|:---:|
| `00` | Network / device-common (NIC, IP, DNS, Bonjour, Wi-Fi, WSD, proxy, Web/IPP) | 56 |
| `01` | Print engine (mode, spool, AppleTalk print object, NetBEUI printer name) | 8 |
| `02` | Model / identity (ModelName, ConnectType) | 3 |
| `03` | Services & security (SNMP, EAP-802.1X, cert store, admin, HTTPS, EpsonNet services) | 81 |
| `04` | Fax / peripheral I/O (Network TWAIN) | 2 |
| `05` | Storage (SharedName / shared-folder access on MFPs with storage) | 3 |

Cat 03 is by far the biggest, largely because it enumerates the on-device
**certificate store** — 16 identical slots each of installed certs and CA
certs, each with a paired fingerprint/key record.

## 6. Field catalog

Each row is one `(category, item, sub)` tuple. "Primary field(s)" lists the
top-level field names within the item (before any array index). "Sub-fields"
counts all fully-qualified paths under that item. "In templates" is how many
device-family templates expose the item.

### Category `00` — Network / device-common (NIC, IP, DNS, Bonjour, Wi-Fi, WSD, proxy, Web/IPP)

56 items in this category.

| Item | Sub | Primary field(s) | Sub-fields | In templates | Sample templates |
|------|-----|------------------|:---:|:---:|-------------|
| `0003` | — | `NICLocation` | 2 | 7 | AONI,DNI,DNI41,DNI42 |
| `0004` | — | `NICManager` | 2 | 5 | DA,DA41,DA42,DA44 |
| `0005` | 07 | `HardwareType,NICEnableOperation,SoftwareType` | 3 | 16 | AOA,DA,DA41,DA42 |
| `0006` | — | `NICAutoPhysical,NICManualPhysical,NICPhysical,NICPhysicalSetup,NICPortType, … (+2)` | 24 | 11 | AONN,DNN,DNN41,DNN42 |
| `0008` | — | `CurrentTopology` | 1 | 5 | AONI,DNI41,DNI42,DNI45 |
| `000A` | — | `AttributePeculiarDeviceName,CommonDeviceName,ReflectCommonDeviceName` | 3 | 48 | AOA,AONI,DA,DA41 |
| `000B` | — | `MSFileSharedOperationType,SupportProtocolOperation` | 3 | 8 | DNM,DNM41,DNM42,DNM45 |
| `000C` | — | `ReflectCommonLocation` | 1 | 7 | AOA,DA,DA41,DA42 |
| `0010` | — | `EnableIP,Gateway,IPAddress,NICAutoIP,NICSupportIP,SubnetMask` | 6 | 9 | AONT,DNT,DNT41,DNT42 |
| `0011` | — | `DNSRequest,DomainName,DomainType,HostName` | 4 | 10 | AONT,DNT,DNT41,DNT42 |
| `0012` | 16 | `DNSServerList,EnableDDNS,GetDNSServer,SetupDNSServerList,SupportDNSServerList, … (+1)` | 21 | 11 | AONT,DNT,DNT41,DNT42 |
| `0013` | — | `EnableRendezvous,SupportPrinterType` | 2 | 12 | AONT,DNT,DNT41,DNT42 |
| `0014` | — | `BonjourLocation,RendezvousName` | 2 | 10 | AONT,DNT,DNT41,DNT42 |
| `0015` | — | `PrinterNum` | 17 | 10 | AONT,DNT,DNT41,DNT42 |
| `0018` | — | `AutoDomainType,DNSRequest,DomainName,DomainType,HostName` | 5 | 8 | AONT,DNT,DNT41,DNT42 |
| `0019` | — | `EnableSNMPv1v2` | 2 | 3 | AOA,DA44,LA45 |
| `0020` | — | `EnableIPX,FrameType` | 2 | 1 | PM1 |
| `0023` | — | `EnableTimeServer,SupportAddressType,SupportMaxInterval,SupportMinInterval,SupportReplyData, … (+6)` | 12 | 1 | DNE45_BS |
| `0030` | — | `EnableAppleTalk,NICATNetID,NetworkID,ZoneName` | 5 | 5 | DNA,DNA42,DNA45,PQ1 |
| `0040` | 41 | `DomainName,EnableNetBEUI,NodeName` | 3 | 10 | DNM,DNM41,DNM42,DNM45 |
| `0050` | — | `PhysicalType` | 1 | 9 | AONI,DNI,DNI41,DNI42 |
| `0051` | — | `APDensity,AuthenticationAlgorithm,Channel,DataRate,DefaultWEPKey, … (+11)` | 16 | 8 | AONN,DNN,DNN41,DNN42 |
| `0053` | — | `Channel,ConnectAPAddress,DataRate,NoiseLevel,SSID,STAMode,SignalLevel` | 7 | 1 | PB1 |
| `0054` | — | `ESSIDCount` | 26 | 9 | AONN,DNN,DNN41,DNN42 |
| `0055` | — | `IBSSIDCount` | 30 | 10 | AONN,DNN,DNN41,DNN42 |
| `0056` | — | `ESSID,ESSIDCount,SignalLevelType` | 23 | 10 | AONN,DNN,DNN41,DNN42 |
| `0057` | — | `ESSID` | 21 | 10 | AONN,DNN,DNN41,DNN42 |
| `0058` | — | `IBSSID,IBSSIDCount` | 8 | 10 | AONN,DNN,DNN41,DNN42 |
| `0059` | — | `IBSSID` | 7 | 10 | AONN,DNN,DNN41,DNN42 |
| `005A` | — | `SSIDListRefresh,SSIDListRefreshReply` | 2 | 8 | AONN,DNN,DNN41,DNN42 |
| `0060` | — | `APDensity,FragmentThreshold,FragmentThresholdMode,HiddenStationMode,PowerManagement, … (+4)` | 9 | 9 | AONI,AONN,DNI,DNI41 |
| `0061` | — | `Channel,ConnectAPAddress,DataRate,NoiseLevel,SSID,STAMode,SignalLevel,SignalStatus` | 8 | 7 | AONI,DNI,DNI41,DNI42 |
| `0062` | — | `802_11Mode,Channel,DataRate,SetupModeNum` | 36 | 9 | AONN,DNN,DNN41,DNN42 |
| `0063` | — | `APDensity,FragmentThreshold,FragmentThresholdMode,HiddenStationMode,PowerManagement, … (+4)` | 9 | 9 | AONN,DNN,DNN41,DNN42 |
| `0064` | — | `AdhocSupportSecurity,AuthenticationAlgorithm,DefaultWEPKey,SecurityMode,SupportAuthenticationAuto, … (+1)` | 6 | 13 | AONN,DNN,DNN41,DNN42 |
| `0065` | — | `Password,PasswordLength` | 2 | 12 | AONN,DNN,DNN41,DNN42 |
| `0066` | — | `SecurityMode` | 1 | 8 | AONN,DNN41,DNN42,DNN44 |
| `0068` | — | `Account,Password` | 2 | 1 | PE1 |
| `0069` | — | `EnableWirelessLAN` | 2 | 11 | AONN,DNN,DNN41,DNN42 |
| `006A` | — | `WPSDeviceName` | 1 | 7 | AONN,DNN,DNN41,DNN42 |
| `0070` | — | `EnableProxy,ProxyAddress,ProxyPortNum,ProxySupportMember` | 5 | 9 | AONT,DNT,DNT41,DNT42 |
| `0071` | — | `ProxyUserName` | 1 | 7 | AONT,DNT,DNT41,DNT42 |
| `0072` | — | `ProxyPassword` | 1 | 7 | AONT,DNT,DNT41,DNT42 |
| `0080` | — | `NICSupportRally` | 1 | 7 | AONT,DNT,DNT41,DNT42 |
| `0081` | — | `EnableWSD,EnableWSDAttribute,WSDDeviceName,WSDLocation` | 4 | 13 | AONT,DNT,DNT41,DNT42 |
| `0082` | — | `EnableLLTD,LLTDDeviceName` | 2 | 9 | AONT,DNT,DNT41,DNT42 |
| `0090` | — | `APDensity,Bandwidth,FragmentThreshold,FragmentThresholdMode,HiddenStationMode, … (+8)` | 13 | 9 | AONN,DNN,DNN41,DNN42 |
| `0091` | — | `SupportChannelList` | 120 | 9 | AONN,DNN,DNN41,DNN42 |
| `0092` | — | `SupportDataRate,SupportDataRateNum` | 33 | 9 | AONN,DNN,DNN41,DNN42 |
| `0093` | — | `Channel,Channel2,ConnectAPAddress,ModeName,RecvDataRate, … (+4)` | 9 | 11 | AONI,AONT,DNI,DNI41 |
| `0094` | — | `802_11Mode,Channel,DataRateParam1,DataRateParam2,DataRateType, … (+4)` | 64 | 9 | AONN,DNN,DNN41,DNN42 |
| `0095` | — | `SetupMode` | 60 | 9 | AONN,DNN,DNN41,DNN42 |
| `0096` | — | `APDensity,Bandwidth,FragmentThreshold,FragmentThresholdMode,HiddenStationMode, … (+5)` | 10 | 9 | AONN,DNN,DNN41,DNN42 |
| `0097` | — | `WifiDirect` | 1 | 3 | AONN,DNN42,DNN44 |
| `0098` | — | `ClientMACAddress1..ClientMACAddress10` | 11 | 5 | AONI,DNI,DNI41,DNI42 |
| `0100` | — | `SupportCommonDeviceFunction,SupportESSIDList,SupportFactoryResetFunction,SupportIBSSIDList,SupportIPInformation, … (+2)` | 7 | 8 | AOA,AONI,AONN,AONT |

### Category `01` — Print engine (mode, spool, AppleTalk print object, NetBEUI printer name)

8 items in this category.

| Item | Sub | Primary field(s) | Sub-fields | In templates | Sample templates |
|------|-----|------------------|:---:|:---:|-------------|
| `0000` | — | `PSVNB` | 1 | 5 | DNM,DNM41,DNM42,DNM45 |
| `0001` | — | `PrinterService` | 1 | 15 | AOA,DA,DA41,DA42 |
| `0002` | — | `ConnectCheck,PrinterSupportFunction` | 2 | 7 | AONT,DNT,DNT41,DNT42 |
| `0021` | — | `PrintMode` | 1 | 1 | PM1 |
| `0030` | — | `PrinterObjectName` | 1 | 5 | DNA,DNA42,DNA45,PQ1 |
| `0031` | — | `EntityType` | 1 | 5 | DNA,DNA42,DNA45,PQ1 |
| `0032` | — | `EntityType1..EntityType15` | 15 | 5 | DNA,DNA42,DNA45,PQ1 |
| `0040` | 41 | `PrinterDeviceName` | 1 | 6 | DNM,DNM41,DNM42,DNM45 |

### Category `02` — Model / identity (ModelName, ConnectType)

3 items in this category.

| Item | Sub | Primary field(s) | Sub-fields | In templates | Sample templates |
|------|-----|------------------|:---:|:---:|-------------|
| `0000` | — | _(base)_ | 1 | 7 | AONI,DNI,DNI41,DNI42 |
| `0000` | 01 | `ConnectType,ModelName` | 2 | 16 | AONI,DNA,DNA42,DNA45 |
| `0001` | — | _(base)_ | 1 | 7 | AONI,DNI,DNI41,DNI42 |

### Category `03` — Services & security (SNMP, EAP-802.1X, cert store, admin, HTTPS, EpsonNet services)

81 items in this category.

| Item | Sub | Primary field(s) | Sub-fields | In templates | Sample templates |
|------|-----|------------------|:---:|:---:|-------------|
| `0000` | — | _(base)_ | 1 | 6 | AONI,DNI,DNI41,DNI42 |
| `0000` | 01 | `InterfaceName,ModelName,PrinterID,PrinterType,PrinterVer` | 5 | 8 | AOA,AONI,DA,DA41 |
| `0001` | — | _(base)_ | 1 | 6 | AONI,DNI,DNI41,DNI42 |
| `0131` | — | `PSServiceEnableH,PSServiceEnableL,PSServiceEnableSupportH,PSServiceEnableSupportL,PSServiceSupportH, … (+1)` | 7 | 5 | AONC,AONT,DNC,DNT41 |
| `0140` | — | `PublicCommunityEnable,ReadOnlyCommunity,ReadWriteCommunity` | 4 | 2 | AONS,DNS |
| `0150` | — | `SupportEAPType` | 1 | 6 | AONN,DNN41,DNN42,DNN44 |
| `0151` | — | `AnonymousName,CACertNum,CASignedCertNum,EAPType,EnableServerAuth, … (+4)` | 9 | 5 | DNN41,DNN42,DNN44,DNN45 |
| `0152` | — | `Password` | 1 | 5 | DNN41,DNN42,DNN44,DNN45 |
| `0160` | — | `SupportCACertNum,SupportCASignedCertNum,SupportCertType` | 3 | 4 | DNAU41,DNAU42,DNAU44,DNAU45 |
| `0161` | — | `CommonName,Issure,Status,Subject,ValidityNotAfter,ValidityNotBefore` | 16 | 4 | DNAU41,DNAU42,DNAU44,DNAU45 |
| `0162` | — | `FingerPrint,FingerPrintLength,KeyLength,KeyUsage,Purpose, … (+4)` | 9 | 4 | DNAU41,DNAU42,DNAU44,DNAU45 |
| `0163` | — | `CommonName,KeyLength,OrganizationName,SupportReplyData,SupportSetupData, … (+6)` | 13 | 2 | DNAU453_BS,DNAU45_BS |
| `0170`–`017F` | — | `CommonName,Issure,Status,Subject,ValidityNotAfter,ValidityNotBefore` | 16 | 4 | _16 identical slots — server / installed cert (0..15)_ |
| `0180`–`018F` | — | `FingerPrint,FingerPrintLength,KeyLength,KeyUsage,Purpose, … (+4)` | 9 | 4 | _16 identical slots — server / installed cert fingerprint+key (0..15)_ |
| `0190`–`019F` | — | `CertificateType,CommonName,Issure,Status,Subject,ValidityNotAfter,ValidityNotBefore` | 17 | 4 | _16 identical slots — CA cert (0..15)_ |
| `0200`–`020F` | — | `FingerPrint,FingerPrintLength,KeyLength,KeyUsage,Purpose, … (+4)` | 9 | 4 | _16 identical slots — CA cert fingerprint+key (0..15)_ |
| `0211` | — | `Mangement_URL` _(sic; Epson misspelling)_ | 4 | 4 | DNAU41,DNAU42,DNAU44,DNAU45 |
| `0212` | — | `DeleteCertNumber,DeleteCertReply,DeleteCertType` | 3 | 2 | DNAU453_BS,DNAU45_BS |
| `0215` | — | `BrowserURL1,BrowserURL2,BrowserURL3` | 4 | 1 | DNAU453_BS |
| `0220` | — | `DefaultTime,SupportMaxYear,SupportMinYear` | 7 | 1 | DNE45_BS |
| `0230` | — | `SslServerAutoRedirectToHttpsEnable,SslServerEncryptionStrength,SslServerSessionCache,SupportReplyData,SupportSetupData, … (+1)` | 7 | 2 | DNAU453_BS,DNAU45_BS |

### Category `04` — Fax / peripheral I/O (Network TWAIN)

2 items in this category.

| Item | Sub | Primary field(s) | Sub-fields | In templates | Sample templates |
|------|-----|------------------|:---:|:---:|-------------|
| `0000` | — | `ConnectType,ModelName` | 2 | 11 | AONI,DNI,DNI41,DNI42 |
| `0010` | — | `NetworkTwain` | 2 | 4 | DS,DS41,DS42,PV1 |

### Category `05` — Storage (SharedName / shared-folder access on MFPs with storage)

3 items in this category.

| Item | Sub | Primary field(s) | Sub-fields | In templates | Sample templates |
|------|-----|------------------|:---:|:---:|-------------|
| `0010` | — | `SharedName` | 1 | 1 | PP1 |
| `0011` | — | `AccessAttribute,EnableShared,SharedName` | 4 | 8 | DNM,DNM41,DNM42,DNM45 |
| `0013` | — | `EnableShared,SharedNameSize,SharedStorageNum` | 24 | 7 | DNM,DNM41,DNM42,DNM45 |

## 7. Templates

EpsonNet Config V4 ships **278 device-family templates** (SettingItems), **265
DeviceInfo files**, and **277 DUIInfo files** in
`.../TemplateSet/Default/`. Each template is identified by a **short code**
(e.g. `PY42`, `DNAU45`, `DNM41`) that the device announces in its
`GetListInfo` reply as its "Template set".

The template code is **not** the product marketing name. To pick a template,
the client compares the incoming `ENPC.02_0000_01.ModelName` string against
`Peculiar.dat`, which contains the model→template mapping. The XP-442 doesn't
appear as a literal string anywhere in the shipped `.dat` files — the mapping
is done by exact match on what the *device* reports over ENPC, not on a
marketing-name table.

Template naming pattern (inferred from clusters):

| Prefix | Rough family |
|---|---|
| `PY` | Expression Home / small inkjets (XP-100..XP-8xx family lives here — `PY39`–`PY48` are the closest matches to XP-440/442's era) |
| `PE` | Photo/Expression Premium |
| `PF`/`PG` | Larger photo / All-in-One |
| `DN*` | Networked printers with fuller feature sets (Ethernet, LDAP, EAP) |
| `DNAU*` | DN variants exposing the full **cert store** (items `0160`–`020F`) |
| `AONN`/`AONT`/`AONI`/`AONS`/`AON*` | "Add-on network" personality bundles (Wi-Fi, TCP/IP, cert, etc.) |
| `DA`/`DC`/`DF`/`DP`/`DS` | DFP / scanner / fax personalities |
| `LA45`/`LC45`/`LN*45` | Large-format / L-series |
| `RS`/`RS1`/`RS2` | Receipt / small printer |
| `FX*` | Fax-capable MFPs |
| `SCF`/`SCM`/`SCN` | Scanner-centric |

To identify which template the **XP-442** actually uses in the field, capture
one `GetListInfo` reply and read the `Template set=` string in
`CENCMNIFInfo::execGetListInfoENPC() : GetListInfo done.(Supported the NIF.) : Template set=`.
The nearest neighbours structurally (Cat 00 items 0090–0096, 0064–0069, plus
Bonjour/WSD) look like `PY39`–`PY48`, `DNAU44`, and `DNAU45`, which are also
the templates that share the certificate slots described in Cat 03.

Beyond `.dat` templates there are matching `.ens` files (magic `ENSCRIPT`
after the ENDU container) — those carry compiled per-template scripts for
validation and dynamic UI, referenced by `EENScriptException` /
`EENScriptCompileException` at runtime.

## 8. Container format: ENDU

All Epson template files (`.dat` and `.ens`) use one binary container:

```
Offset  Bytes                             Meaning
0x00    'E' 'N' 'D' 'U'                   Magic
0x04    0x01 0x00 0x00 0x00 …             Fixed padding to 0x40
0x40    01 00 00 00 <length:BE32>         Section header (length = bytes from 0x50)
0x50    <body>                            Tokenized-XML body of `length` bytes
```

The body is a **tokenized XML** stream:

- **String table** — length-prefixed strings, in the order they first appear
  in the document. If any byte of the string ≥ `0x80`, the whole string is
  encoded as **bitwise complement (XOR `0xFF`)** of ASCII. Otherwise it's plain.
  Every URI, namespace, model code and dtype value is XOR'd; element and
  attribute names are plain.
- **Structure tokens** referencing the string table by index:
  - `10 00 XX` — open element `[XX]`
  - `11 00 XX` — close element `[XX]` (`00` = end of root)
  - `20 00 XX` — attribute name `[XX]`
  - `00 XX`    — inline value referring to string `[XX]`
  - `30 00 XX` — text / child-region marker with tag `[XX]`
  - `02 00 <len> <bytes>` — inline literal (used for XML prolog version)
  - `03 00 <len> <bytes>` — inline literal (used for XML prolog encoding)
  - `0a`, `20` — whitespace / indent (cosmetic only)

So the schema is **not** encrypted, just tokenized and lightly obfuscated —
one XOR-`0xFF` pass on high-byte runs recovers everything.

## 9. Reproducing the decode

Two scripts at the repo root:

- [build_enpc_catalog.py](build_enpc_catalog.py) — the pipeline (ENDU decoder + catalog builder + item roll-up), a single self-contained Python file with no dependencies beyond the standard library.
- [build_enpc_catalog.sh](build_enpc_catalog.sh) — thin wrapper that finds EpsonNet Config's bundled `TemplateSet/Default/` inside `/Applications/...` and rebuilds `./enpc_schema/`.

```bash
# Full rebuild against the installed app (recreates enpc_schema/)
./build_enpc_catalog.sh

# Or point at a non-standard install
EPSONNET_APP=/path/to/EpsonNet\ Config.app ./build_enpc_catalog.sh

# Same thing directly, e.g. against a copy of the templates elsewhere
python3 build_enpc_catalog.py all \
  "/Applications/Epson Software/EpsonNet/EpsonNet Config V4/EpsonNet Config.app/Contents/Resources/Settings/TemplateSet/Default" \
  enpc_schema

# Dev convenience: decode one file to stdout
python3 build_enpc_catalog.py decode \
  "/Applications/.../TemplateSet/Default/SettingItems_UC.dat"
```

The two TSVs are byte-reproducible from the shipped template set — the pipeline
is deterministic.

## 10. Cross-references to firmware findings

| Finding here | Corroborates / extends |
|---|---|
| MD5 challenge-response for `execLoginENPC` | [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md)'s note that ENPC frame format was un-reversed — auth mechanism now confirmed as plain MD5, no TLS |
| Cooperative `LockMode`/`LockTimeout` around `SetDetail` | Matches `COMIF_LEVT_LOCKFLASH` / `UNLOCKFLASH` events in [ARCHITECTURE.md](ARCHITECTURE.md) |
| ENPC `(cat, item)` = curated subset of PNRSC | Same *shape* as PNRSC (category + numeric item), but not literally the same numbers — ENPC caps at cat `05`, PNRSC uses subjects up to `0x37`+. A remap table in `PNENPCServiceStart` translates wire IDs into PNRSC `(subject, item)`. Practical consequence: the ENPC catalog is the **network-exposed attack surface**; PNRSC entries with no ENPC counterpart (telnet, flash control, …) are only reachable via COMIF/USB/panel — see [PNRSC_CONFIG_REGISTRY.md](PNRSC_CONFIG_REGISTRY.md) |
| Category 03 items 0160–020F = cert store | Explains the size and layout of the certificate storage referenced by `CA-SIGNED` at `0x981dc7` in [FIRMWARE_LAYOUT.md](FIRMWARE_LAYOUT.md) |
| Category 00 item 0100 `SupportPasswordAuthType` | The bit the client uses to decide between `execLoginENPC` and plain read/write |
| `FactResetUnlock` step before `FactResetPacket` | Worth checking whether this gate is client-only or also enforced by `PNENPCServiceStart` in `nwsoc` |
| Same field space over ENPC (UDP 3289) *and* SOAP (WebConfigEx over HTTP) | Two attack surfaces to the same registry; both should be considered in any hardening review |

## 11. What's still open

Recommended next avenue is the **WSD SOAP path** (§2) — it removes the
biggest unknown (UDP packet framing) and reduces the problem to standard
SOAP tooling plus a WSD callback wiring detail.

- **WSD response-delivery mode** (highest-value single unknown, blocks the SOAP
  path). GetData returns 202 with empty body when `ReplyTo=anonymous`. Two
  hypotheses to distinguish:
  1. GetData requires input parameters and returns silently on empty. Fix:
     recover the request-body shape from the EpsonNet Config client (the
     `execGetDetailSOAP` code path builds these).
  2. The server always delivers responses async to a real callback URL. Fix:
     stand up a tiny HTTP listener and set `wsa:ReplyTo` to it.
- **SOAP auth requirements** — no 401 on GetData without creds, but SetData /
  SetFile may need the WebConfigEx session cookie or WS-Security. Test after
  the response-delivery mode is understood.
- **Packet framing for the UDP path** — the class names `CENPCPacketFormat` /
  `CENPCPacketDataFormat` are visible but the layout isn't recovered here. Less
  urgent now that the SOAP path is available; still needed for the LAN-broadcast
  discovery probe on 3289 (which SOAP doesn't cover).
- **The MD5 challenge shape** — nonce length, UTF-8 vs Latin-1 password
  encoding, salt vs no salt (for `execLoginENPC`).
- **Whether `FactResetUnlock` is enforced by the receiver** or is a
  client-side courtesy.
- **Full DUI grammar** — the `.ens` files (`ENSCRIPT` container) hold compiled
  scripts driving conditional UI. Not decoded here.
- **XP-442's exact template code** — from static inspection alone the nearest
  templates are `PY39`–`PY48` and `DNAU44`/`45`; a single live `GetListInfo`
  reply (over UDP or SOAP) would settle it.
