import boto3
import os
import re
import sys
import xml.etree.ElementTree as ET
from botocore.exceptions import ClientError, NoCredentialsError
from xml.dom import minidom

ROLE_PATTERNS = [
    "webserver",
    "processing",
    "agswin",
    "agswin-*",
    "portal",
    "portal-*",
]

FILTER_CHUNK_SIZE = 100


def prompt_required(prompt_text):
    while True:
        value = input(prompt_text).strip()
        if value:
            return value
        print("Value is required.\n")


def prompt_int(prompt_text):
    while True:
        try:
            return int(input(prompt_text).strip())
        except ValueError:
            print("Enter a valid number.\n")


def prompt_yes_no(prompt_text):
    while True:
        value = input(prompt_text).strip().lower()
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("Enter Y or N.\n")


def paste_aws_exports():
    print("\nPaste the AWS export block below.")
    print("Press ENTER on a blank line when done.\n")

    lines = []
    while True:
        line = input()
        if line.strip() == "":
            break
        lines.append(line)

    text = "\n".join(lines)

    creds = {}
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        match = re.search(rf'{key}\s*=\s*["\']?([^"\']+)["\']?', text)
        if match:
            creds[key] = match.group(1).strip()

    missing = [
        key for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
        if key not in creds
    ]

    if missing:
        print("\nCould not find required credential values:")
        for key in missing:
            print(f"- {key}")
        sys.exit(1)

    return creds


def chunk_list(items, chunk_size):
    for i in range(0, len(items), chunk_size):
        yield items[i:i + chunk_size]


def build_name_filters_for_range(start_stack, end_stack):
    values = []
    for stack in range(start_stack, end_stack + 1):
        for role in ROLE_PATTERNS:
            values.append(f"prod-{stack}-{role}")
    return values


def build_name_filters_for_all():
    return [
        "prod-*-webserver",
        "prod-*-processing",
        "prod-*-agswin",
        "prod-*-agswin-*",
        "prod-*-portal",
        "prod-*-portal-*",
    ]


def get_tag_value(instance, key_name):
    for tag in instance.get("Tags", []):
        if tag.get("Key", "").lower() == key_name.lower():
            return tag.get("Value")
    return None


def get_name_tag(instance):
    return get_tag_value(instance, "Name")


def stack_sort_key(stack_name):
    try:
        return int(stack_name.split("-")[1])
    except Exception:
        return 999999


def validate_credentials(session):
    try:
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        print("\nAWS authentication successful.")
        print(f"Account: {identity.get('Account')}")
        return True
    except (ClientError, NoCredentialsError) as e:
        print("\nAWS authentication failed.")
        print(e)
        return False


def get_instances(session, name_filters, folder_tag_key=None):
    ec2 = session.client("ec2")
    instances = []

    for filter_chunk in chunk_list(name_filters, FILTER_CHUNK_SIZE):
        response = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": filter_chunk},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )

        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                public_ip = instance.get("PublicIpAddress")
                if not public_ip:
                    continue

                name = get_name_tag(instance)
                if not name:
                    continue

                parts = name.split("-")
                if len(parts) < 3:
                    continue

                folder_tag_value = None
                if folder_tag_key:
                    folder_tag_value = get_tag_value(instance, folder_tag_key)

                instances.append({
                    "stack": "-".join(parts[:2]),
                    "name": name,
                    "folder_tag_value": folder_tag_value,
                    "public_ip": public_ip,
                })

    return sorted(instances, key=lambda x: (stack_sort_key(x["stack"]), x["name"]))


def add_group_properties(parent, name, expanded=False):
    props = ET.SubElement(parent, "properties")
    ET.SubElement(props, "expanded").text = "True" if expanded else "False"
    ET.SubElement(props, "name").text = name


def add_server_properties(parent, display_name, host):
    props = ET.SubElement(parent, "properties")
    ET.SubElement(props, "displayName").text = display_name
    ET.SubElement(props, "name").text = host


def build_rdg(instances, output_file):
    root = ET.Element("RDCMan", {
        "programVersion": "2.93",
        "schemaVersion": "3",
    })

    file_node = ET.SubElement(root, "file")
    ET.SubElement(file_node, "credentialsProfiles")
    add_group_properties(file_node, "AWS Generated", expanded=True)

    stacks = {}
    for instance in instances:
        stacks.setdefault(instance["stack"], []).append(instance)

    for stack_name in sorted(stacks.keys(), key=stack_sort_key):
        group = ET.SubElement(file_node, "group")

        folder_tag_values = sorted({
            i["folder_tag_value"] for i in stacks[stack_name]
            if i.get("folder_tag_value")
        })

        group_name = stack_name
        if folder_tag_values:
            group_name = f"{stack_name} ({folder_tag_values[0]})"

        add_group_properties(group, group_name, expanded=False)

        for instance in stacks[stack_name]:
            server = ET.SubElement(group, "server")
            add_server_properties(server, instance["name"], instance["public_ip"])

    pretty_xml = minidom.parseString(
        ET.tostring(root, encoding="utf-8")
    ).toprettyxml(indent="  ")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(pretty_xml)


def get_output_path():
    while True:
        output_file = prompt_required("\nOutput .rdg path or filename: ")

        if not output_file.lower().endswith(".rdg"):
            output_file += ".rdg"

        if os.path.exists(output_file):
            if not prompt_yes_no(f"File already exists: {output_file}. Overwrite? Y/N: "):
                continue

        return output_file


def choose_scope():
    print("\nSelect stack scope:")
    print("1. All available matching prod stacks")
    print("2. Stack range")
    print("3. Single stack")

    while True:
        choice = input("\nChoice [1-3]: ").strip()

        if choice == "1":
            return build_name_filters_for_all()

        if choice == "2":
            start_stack = prompt_int("Start stack number: ")
            end_stack = prompt_int("End stack number: ")

            if end_stack < start_stack:
                print("End stack cannot be lower than start stack.\n")
                continue

            return build_name_filters_for_range(start_stack, end_stack)

        if choice == "3":
            stack = prompt_int("Stack number: ")
            return build_name_filters_for_range(stack, stack)

        print("Choose 1, 2, or 3.\n")


def choose_folder_tag_key():
    print("\nOptional folder tag:")
    print("Enter an EC2 tag key to append its value to each stack folder.")
    print("Example: Billing -> prod-47 (Customer Name)")
    print("Leave blank to keep folder names as prod-XX.")

    value = input("\nFolder tag key [optional]: ").strip()
    return value if value else None


def main():
    print("\nAWS to RDCMan RDG Generator")
    print("--------------------------")
    print("Credentials are used only for this run and are not saved.")

    creds = paste_aws_exports()
    region = prompt_required("\nAWS Region [example: us-east-1]: ")

    session = boto3.Session(
        aws_access_key_id=creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=creds["AWS_SESSION_TOKEN"],
        region_name=region,
    )

    if not validate_credentials(session):
        sys.exit(1)

    name_filters = choose_scope()
    folder_tag_key = choose_folder_tag_key()
    output_file = get_output_path()

    print("\nQuerying EC2 instances...")

    try:
        instances = get_instances(session, name_filters, folder_tag_key)
    except ClientError as e:
        print("\nEC2 query failed.")
        print(e)
        sys.exit(1)

    print(f"\nFound {len(instances)} running instances with public IPs:\n")

    for instance in instances:
        if folder_tag_key:
            print(f"{instance['stack']} | {instance.get('folder_tag_value') or 'TAG_NOT_FOUND'} | {instance['name']} | {instance['public_ip']}")
        else:
            print(f"{instance['stack']} | {instance['name']} | {instance['public_ip']}")

    if not instances:
        print("\nNo matching instances found. No RDG file created.")
        sys.exit(0)

    build_rdg(instances, output_file)

    print(f"\nCreated: {output_file}")
    print("Open the file in RDCMan and set credentials at the parent level manually.")


if __name__ == "__main__":
    main()