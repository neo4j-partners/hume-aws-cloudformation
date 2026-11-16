"""Generate the ECS pre-baked AMI diagram for the architecture overview."""
import sys
from xml.sax.saxutils import escape

NAVY, GREY, LINE, PANEL, AWS = "#16233A", "#5C6470", "#DAD7CE", "#FAFAF8", "#F1F1EE"
BLUE_FILL, BLUE_LINE = "#DEEAF6", "#1F4E86"
TEAL_FILL, TEAL_LINE = "#CFEDE8", "#156459"

# The images baked into the AMI, in the order ami/create_ami.py pulls them.
IMAGES = ["hume-web", "hume-api", "hume-orchestra", "hume-media", "hume-eventstore",
          "hume-config-cli", "postgres:17-alpine"]
IDLE_NOTE = {
    "hume-config-cli": "one-shot task for the Spine demo only",
    "postgres:17-alpine": "one-shot tasks: db-init, smoke test",
}
RUNNING = ["web", "api", "orchestra", "eventstore", "media"]

out: list[str] = []


def text(x, y, s, size=9, fill=NAVY, weight="400", anchor="start", spacing=None):
    ls = f' letter-spacing="{spacing}"' if spacing else ""
    out.append(f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" font-weight="{weight}" '
               f'text-anchor="{anchor}"{ls}>{escape(s)}</text>')


def rect(x, y, w, h, fill, stroke, rx=8, dash=False, extra=""):
    d = ' stroke-dasharray="4 3"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}"{d}{extra}/>')


def box(x, y, w, h, title, lines, fill, stroke):
    rect(x, y, w, h, fill, stroke)
    text(x + 14, y + 22, title, size=11.5, weight="700")
    for i, line in enumerate(lines):
        text(x + 14, y + 40 + i * 14, line, size=9, fill=GREY)


def section(y, label):
    text(44, y, label, size=8.5, fill=GREY, weight="700", spacing="0.4")
    out.append(f'<line x1="44" y1="{y + 6}" x2="916" y2="{y + 6}" stroke="#E7E4DB"/>')


def arrow(x1, y1, x2, y2, dashed=False):
    d = ' stroke-dasharray="4 3"' if dashed else ""
    out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#8A8A8A" stroke-width="1.6"{d} '
               f'marker-end="url(#arrow)"/>')


def instance(x, y, w, running):
    rect(x, y, w, 268, PANEL, LINE, rx=12)
    text(x + 16, y + 24, "ONE EC2 INSTANCE, IN A PRIVATE SUBNET OF THE NEO4J EE VPC", size=11, weight="700",
         spacing="0.3")
    text(x + 16, y + 40, "Launched from the Hume AMI for this release by an Auto Scaling group of one "
         "(min 1, max 1, desired 1), through an ECS capacity provider", size=9, fill=GREY)
    rect(x + 16, y + 52, w - 32, 40, AWS, LINE)
    text(x + 30, y + 68, "ECS agent", size=10.5, weight="700")
    text(x + 30, y + 83, "prefer-cached: starts each image from local disk, no registry login at boot. "
         "ENI trunking gives each task its own network interface.", size=8.5, fill=GREY)
    text(x + 16, y + 112, "RUNNING CONTAINERS, USING CPU AND MEMORY", size=8, fill=GREY, weight="700", spacing="0.4")
    n = len(running)
    cw = (w - 32 - (n - 1) * 8) / n
    for i, name in enumerate(running):
        cx = x + 16 + i * (cw + 8)
        rect(cx, y + 120, cw, 30, TEAL_FILL, TEAL_LINE, rx=6)
        text(cx + cw / 2, y + 139, name, size=10, weight="700", anchor="middle")
    text(x + 16, y + 172, "IMAGE CACHE ON THE ROOT VOLUME, BAKED INTO THE AMI", size=8, fill=GREY,
         weight="700", spacing="0.4")
    cols = 4
    iw = (w - 32 - (cols - 1) * 8) / cols
    for i, img in enumerate(IMAGES):
        col, row = i % cols, i // cols
        ix, iy = x + 16 + col * (iw + 8), y + 180 + row * 40
        active = img.removeprefix("hume-") in running
        rect(ix, iy, iw, 32, TEAL_FILL if active else "#FFFFFF", TEAL_LINE if active else LINE,
             rx=6, dash=not active)
        text(ix + 10, iy + 14, img, size=9.5, weight="700")
        note = "running" if active else IDLE_NOTE.get(img, "idle on disk, no CPU or memory")
        text(ix + 10, iy + 26, note, size=8, fill=GREY)


out.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="20 20 920 648" '
           'font-family="\'Helvetica Neue\', Helvetica, Arial, sans-serif">')
out.append("<title>How ECS Manages the Pre-Baked EC2 Instance</title>")
out.append('<defs><filter id="sh" x="-10%" y="-10%" width="120%" height="130%">'
           '<feDropShadow dx="0" dy="2" stdDeviation="4" flood-color="#1a1a1a" flood-opacity="0.10"/></filter>'
           '<marker id="arrow" markerWidth="9" markerHeight="9" refX="7.5" refY="4" orient="auto" '
           'markerUnits="userSpaceOnUse"><path d="M0,0 L8,4 L0,8 Z" fill="#8A8A8A"/></marker></defs>')
rect(34, 32, 892, 624, "#FFFFFF", "#E7E4DB", rx=16, extra=' filter="url(#sh)"')

section(58, "1. AWS-MANAGED: THE ECS CONTROL PLANE DECIDES WHAT RUNS, AND CLOUDFORMATION BUILDS IT ALL")
box(44, 72, 280, 88, "ECS cluster", ["Tracks the registered instance",
                                       "Knows its free CPU and memory",
                                       "No charge for the control plane"], BLUE_FILL, BLUE_LINE)
box(340, 72, 280, 88, "ECS services", ["One per Hume service, with a copy count:",
                                        "web, api, orchestra, eventstore, media",
                                        "No Keycloak: sign-in goes to an OIDC provider"],
    BLUE_FILL, BLUE_LINE)
box(636, 72, 280, 88, "CloudFormation stack", ["Creates the cluster, capacity provider,",
                                                "Auto Scaling group, services, ALB, IAM roles",
                                                "Sets ECS_CLUSTER on the new instance"], AWS, LINE)

arrow(480, 162, 480, 256)
text(490, 206, "Start tasks: web, api, orchestra,", size=8.5, fill=GREY)
text(490, 218, "eventstore, media", size=8.5, fill=GREY)
arrow(110, 256, 110, 164, dashed=True)
text(118, 206, "The agent reports its", size=8.5, fill=GREY)
text(118, 218, "free CPU and memory", size=8.5, fill=GREY)
arrow(850, 164, 850, 256, dashed=True)
text(842, 206, "User data joins the", size=8.5, fill=GREY, anchor="end")
text(842, 218, "instance to the cluster", size=8.5, fill=GREY, anchor="end")

instance(44, 260, 872, RUNNING)

rect(44, 544, 872, 100, AWS, LINE)
text(58, 566, "WHERE STATE LIVES", size=8.5, fill=GREY, weight="700", spacing="0.4")
text(58, 582, "Outside the instance: Postgres in RDS, the graph in Neo4j EE, and the secrets in Secrets Manager "
     "under a customer-managed KMS key.", size=9.5)
text(58, 597, "On the instance disk: the config files from the AMI and the truststore file, which "
     "user data writes at boot from its Secrets Manager secret.", size=9.5)
text(58, 612, "On EFS: the media files, Orchestra's data folder and the API audit log. The file system is "
     "encrypted, backed up daily, and kept when the stack is deleted.", size=9.5)
text(58, 627, "If the instance fails, the Auto Scaling group launches a replacement from the AMI and ECS restarts "
     "every task there. It rebuilds the truststore and mounts EFS.", size=9.5)
out.append("</svg>")

with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
