# Diagrams: how everything fits together

[← back to README](../README.md)

Three pictures, drawn with Mermaid, which GitHub shows as pictures when you open this page.

## 1. A day in the life of the schedule

From orbital data to a checked recording. The first box is what you do now and then, the second runs by itself, the third is what you look at.

```mermaid
flowchart LR
    subgraph PLANNING["1. Plan (you, now and then)"]
        direction TB
        TLE["update_tle.py<br/>fresh orbital elements"]
        SAT[("satellites.yaml<br/>frequency, min elevation,<br/>priority")]
        PLAN["plan_passes.py<br/>passes above the<br/>minimum elevation"]
        SCHED[("schedule.yaml<br/>approved / rejected")]
        OV["overlaps: priority decides,<br/>you choose, or pair_passes.py<br/>records both"]
        TLE --> PLAN
        SAT --> PLAN
        PLAN --> SCHED
        SCHED --- OV
    end

    subgraph RUNNING["2. Run (automatic)"]
        direction TB
        RUN["run_passes.py<br/>waits for each pass"]
        PASS["the pass<br/>flowgraph records,<br/>antenna tracks"]
        CHECK["passcheck.py<br/>did it really record?"]
        RUN --> PASS --> CHECK
    end

    subgraph REVIEW["3. Review (you)"]
        direction TB
        LOG[("pass_log.jsonl")]
        STATUS["status.py / dashboard.py<br/>problems shown until you<br/>clear them"]
        SHOW["show_pass_log.py"]
        CLEAN["clean_iq.py<br/>removes old IQ, never one<br/>being recorded"]
        LOG --> STATUS
        LOG --> SHOW
    end

    PLANNING --> RUNNING --> REVIEW
```

More: [Daily workflow](daily-workflow.md), [Scripts reference](scripts-reference.md).

## 2. Where the signal goes, by kind of satellite

The flowgraph is universal. What comes after it is a choice per satellite.

```mermaid
flowchart LR
    ANT["antenna"] --> SDR["SDR<br/>(Airspy, RTL-SDR, ...)"] --> FG["GNU Radio flowgraph<br/>tune + Doppler + demodulate"]

    FG -->|"every satellite"| IQ[("raw IQ file")]
    FG -->|"decoding satellites"| FR["decoded frames"]
    FR --> KSS[(".kss file")]
    FR --> REL["relay.py<br/>long-running, one per satellite"]
    REL --> DEC["your decoder<br/>(for example SatsDecoder)"]
    FG -->|"extra_outputs: zeromq_pub"| APP1["any app that subscribes"]
    FG -->|"extra_outputs: tcp_bridge"| BR["tcp_bridge.py<br/>stays connected between passes"]
    BR --> APP2["image viewer, etc."]
```

`relay.py` and `tcp_bridge.py` exist because a flowgraph lives for one pass, while a decoder window is meant to stay open.
More: [Architecture](architecture.md), [Adding a satellite](adding-satellites.md).

## 3. Looking at the other computer's dashboard

Each computer serves a read-only status page on itself (port 8750). A tunnel it opens outward to your server makes it reachable from the other computer.
Nothing is opened to the internet.

```mermaid
flowchart LR
    subgraph C1["Computer 1"]
        D1["dashboard.py<br/>127.0.0.1:8750"] --- T1["tunnel service<br/>(outgoing SSH)"]
    end
    subgraph C2["Computer 2"]
        D2["dashboard.py<br/>127.0.0.1:8750"] --- T2["tunnel service<br/>(outgoing SSH)"]
    end
    subgraph SRV["Your server (nothing opened to the internet)"]
        P1["port 18750<br/>its own loopback only"]
        P2["port 18751<br/>its own loopback only"]
    end
    T1 -->|"ssh -R"| P1
    T2 -->|"ssh -R"| P2
    V["gtview on either computer<br/>-> your browser"] -->|"ssh -L<br/>(key only)"| SRV
```

More: [Remote dashboard](remote-dashboard.md).
