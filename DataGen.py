#!/usr/bin/env python3
"""Generate ui/data/data.json from Satisfactory's localized Docs JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / "ui" / "data" / "data.json"

ITEM_GROUPS = (
    "FGItemDescriptor",
    "FGItemDescriptorBiomass",
    "FGItemDescriptorNuclearFuel",
)
MACHINE_GROUPS = (
    "FGBuildableManufacturer",
    "FGBuildableManufacturerVariablePower",
)
GENERATOR_GROUPS = (
    "FGBuildableGeneratorFuel",
    "FGBuildableGeneratorNuclear",
)
VARIABLE_POWER_GROUP = "FGBuildableManufacturerVariablePower"

SKIPPED_RECIPES = {
    "Recipe_PowerCrystalShard_1_C",
    "Recipe_PowerCrystalShard_2_C",
    "Recipe_PowerCrystalShard_3_C",
}

CLASS_NAME_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*_C)")
ITEM_AMOUNT_RE = re.compile(
    r'ItemClass="[^"]*[./]([A-Za-z][A-Za-z0-9_]*_C)\'",Amount=([-+0-9.eE]+)'
)

POWER_ITEMS = {
    "Power_Produced": {"name": "Power (Total)", "points": 0.0},
    "Power_Produced_Other": {"name": "Power (Other)", "points": 0.0},
    "Power_Produced_Fuel": {"name": "Power (Fuel)", "points": 0.0},
    "Power_Produced_Nuclear": {"name": "Power (Nuclear)", "points": 0.0},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Satisfactory Solver data from a localized "
            "CommunityResources/Docs JSON file such as en-US.json."
        )
    )
    parser.add_argument(
        "json_file",
        type=Path,
        help="path to the source JSON file, for example CommunityResources/Docs/en-US.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="output JSON path (default: ui/data/data.json)",
    )
    return parser.parse_args()


def group_name(native_class: str) -> str:
    return native_class.rsplit(".", 1)[-1].rstrip("'")


def clean_number(value: float) -> int | float:
    rounded = round(value)
    return rounded if abs(value - rounded) < 1e-9 else value


def load_source(path: Path) -> list[dict]:
    if path.suffix.lower() != ".json":
        raise ValueError("the input file must have a .json extension")
    if not path.is_file():
        raise ValueError(f"input file does not exist: {path}")

    raw = path.read_bytes()
    for encoding in ("utf-16", "utf-8-sig"):
        try:
            data = json.loads(raw.decode(encoding))
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    else:
        raise ValueError("input is not valid UTF-16 or UTF-8 JSON")

    if not isinstance(data, list):
        raise ValueError("expected a Docs JSON array at the document root")
    if not all(
        isinstance(group, dict) and "NativeClass" in group and "Classes" in group
        for group in data
    ):
        raise ValueError("input does not have the expected Satisfactory Docs JSON structure")
    return data


def index_groups(source: list[dict]) -> dict[str, list[dict]]:
    return {group_name(group["NativeClass"]): group["Classes"] for group in source}


def descriptor_record(descriptor: dict) -> dict:
    energy = float(descriptor.get("mEnergyValue", 0))
    if descriptor["mForm"] in {"RF_LIQUID", "RF_GAS"}:
        energy *= 1000
    return {
        "name": descriptor["mDisplayName"],
        "energy": energy,
        "form": descriptor["mForm"],
        "points": int(descriptor.get("mResourceSinkPoints", 0)),
    }


def parse_item_amounts(value: str, descriptors: dict[str, dict]) -> list[dict]:
    result = []
    matches = ITEM_AMOUNT_RE.findall(value)
    if value.count("ItemClass=") != len(matches):
        raise ValueError(f"could not parse item amounts: {value}")
    for item_id, raw_amount in matches:
        amount = float(raw_amount)
        descriptor = descriptors.get(item_id)
        if descriptor and descriptor.get("mForm") in {"RF_LIQUID", "RF_GAS"}:
            amount /= 1000
        result.append({"item": item_id, "amount": clean_number(amount)})
    return result


def machine_records(groups: dict[str, list[dict]]) -> tuple[dict, dict]:
    machines = {}
    machine_sources = {}

    for source_group in (*MACHINE_GROUPS, *GENERATOR_GROUPS):
        for machine in groups.get(source_group, []):
            machine_id = machine["ClassName"]
            is_generator = source_group in GENERATOR_GROUPS
            machines[machine_id] = {
                "name": machine["mDisplayName"],
                "power_use": (
                    0
                    if is_generator
                    else (
                        (
                            float(machine["mEstimatedMininumPowerConsumption"])
                            + float(machine["mEstimatedMaximumPowerConsumption"])
                        )
                        / 2
                        if source_group == VARIABLE_POWER_GROUP
                        else float(machine["mPowerConsumption"])
                    )
                ),
                "power_produced": (
                    float(machine["mPowerProduction"]) if is_generator else 0
                ),
            }
            machine_sources[machine_id] = machine

    return machines, machine_sources


def select_recipes(
    groups: dict[str, list[dict]],
    machine_ids: set[str],
) -> list[tuple[dict, str]]:
    selected = []
    for recipe in groups.get("FGRecipe", []):
        if recipe["ClassName"] in SKIPPED_RECIPES:
            continue
        produced_in = set(CLASS_NAME_RE.findall(recipe.get("mProducedIn", "")))
        supported = produced_in & machine_ids
        if len(supported) > 1:
            raise ValueError(
                f"{recipe['ClassName']} has multiple supported production machines"
            )
        if supported:
            selected.append((recipe, supported.pop()))
    return selected


def recipe_power(recipe: dict, machine_id: str, machines: dict) -> float:
    constant = float(recipe.get("mVariablePowerConsumptionConstant", 0))
    factor = float(recipe.get("mVariablePowerConsumptionFactor", 0))
    if constant or factor > 1:
        return constant + factor / 2
    return float(machines[machine_id]["power_use"])


def build_recipes(
    selected: list[tuple[dict, str]],
    descriptors: dict[str, dict],
    machines: dict,
) -> dict:
    recipes = {}
    for source, machine_id in selected:
        recipe_id = source["ClassName"]
        name = source["mDisplayName"]
        recipes[recipe_id] = {
            "name": name,
            "time": float(source["mManufactoringDuration"]),
            "ingredients": parse_item_amounts(source["mIngredients"], descriptors),
            "products": parse_item_amounts(source["mProduct"], descriptors),
            "machine": machine_id,
            "power_use": recipe_power(source, machine_id, machines),
        }
    return recipes


def generator_power_item(generator_id: str) -> str:
    if "Nuclear" in generator_id:
        return "Power_Produced_Nuclear"
    if generator_id == "Build_GeneratorFuel_C":
        return "Power_Produced_Fuel"
    return "Power_Produced_Other"


def build_generator_recipes(
    generators: dict[str, dict],
    descriptors: dict[str, dict],
) -> dict:
    recipes = {}
    for generator_id, generator in generators.items():
        power = float(generator["mPowerProduction"])
        fuel_load = float(generator["mFuelLoadAmount"])
        supplemental_ratio = float(generator.get("mSupplementalToPowerRatio", 0))

        for fuel in generator.get("mFuel", []):
            fuel_id = fuel["mFuelClass"]
            descriptor = descriptors.get(fuel_id)
            if descriptor is None:
                raise ValueError(f"missing descriptor for generator fuel {fuel_id}")

            fuel_amount = fuel_load
            if descriptor["mForm"] in {"RF_LIQUID", "RF_GAS"}:
                fuel_amount /= 1000

            energy = float(descriptor["mEnergyValue"])
            if descriptor["mForm"] in {"RF_LIQUID", "RF_GAS"}:
                energy *= 1000
            duration = energy * fuel_amount / power
            power_amount = power * duration / 60
            category_item = generator_power_item(generator_id)

            ingredients = [
                {"item": fuel_id, "amount": clean_number(fuel_amount)}
            ]
            supplemental_id = fuel.get("mSupplementalResourceClass", "")
            if supplemental_id:
                supplemental_amount = power * supplemental_ratio * duration / 1000
                ingredients.append(
                    {
                        "item": supplemental_id,
                        "amount": clean_number(supplemental_amount),
                    }
                )

            products = [
                {"item": category_item, "amount": clean_number(power_amount)},
                {"item": "Power_Produced", "amount": clean_number(power_amount)},
            ]
            byproduct = fuel.get("mByproduct", "")
            if byproduct:
                products.append(
                    {
                        "item": byproduct,
                        "amount": clean_number(float(fuel["mByproductAmount"])),
                    }
                )

            recipes[f"{generator_id}_{fuel_id}"] = {
                "name": f"{generator['mDisplayName']} ({descriptor['mDisplayName']})",
                "time": float(duration),
                "ingredients": ingredients,
                "products": products,
                "machine": generator_id,
                "power_use": 0.0,
            }
    return recipes


def generate(source: list[dict]) -> dict:
    groups = index_groups(source)
    descriptors = {
        item["ClassName"]: item
        for classes in groups.values()
        for item in classes
        if "mDisplayName" in item and "mForm" in item and "mResourceSinkPoints" in item
    }

    resources = {
        item["ClassName"]: descriptor_record(item)
        for item in groups.get("FGResourceDescriptor", [])
    }
    machines, machine_sources = machine_records(groups)
    generator_ids = [
        item["ClassName"]
        for source_group in GENERATOR_GROUPS
        for item in groups.get(source_group, [])
    ]
    manufacturing_ids = set(machines) - set(generator_ids)

    selected = select_recipes(groups, manufacturing_ids)
    recipes = build_recipes(selected, descriptors, machines)
    recipes.update(
        build_generator_recipes(
            {key: machine_sources[key] for key in generator_ids},
            descriptors,
        )
    )

    referenced_items = {
        part["item"]
        for recipe in recipes.values()
        for side in ("ingredients", "products")
        for part in recipe[side]
    }
    base_item_ids = {
        item["ClassName"]
        for source_group in ITEM_GROUPS
        for item in groups.get(source_group, [])
        if not item["ClassName"].startswith("Desc_XmasDataCartridge")
    }
    item_ids = (base_item_ids | referenced_items) - set(resources) - set(POWER_ITEMS)

    missing = sorted(item_ids - set(descriptors))
    if missing:
        raise ValueError(f"missing item descriptors: {', '.join(missing)}")

    items = {
        item_id: descriptor_record(descriptors[item_id])
        for item_id in descriptors
        if item_id in item_ids
    }
    items.update(POWER_ITEMS)

    return {
        "items": items,
        "resources": resources,
        "recipes": recipes,
        "machines": machines,
        "generators": {},
    }


def write_output(data: dict, path: Path) -> None:
    if path.suffix.lower() != ".json":
        raise ValueError("the output file must have a .json extension")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def display_path(path: Path) -> Path:
    try:
        return path.resolve().relative_to(SCRIPT_DIR)
    except ValueError:
        return path


def main() -> int:
    args = parse_args()
    try:
        input_path = args.json_file.expanduser()
        output = args.out.expanduser()
        if input_path.resolve() == output.resolve():
            raise ValueError("input and output paths must be different")
        source = load_source(input_path)
        data = generate(source)
        write_output(data, output)
    except (OSError, ValueError) as exc:
        print(f"DataGen.py: error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Generated {display_path(output)} "
        f"({len(data['items'])} items, {len(data['resources'])} resources, "
        f"{len(data['recipes'])} recipes, {len(data['machines'])} machines)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
