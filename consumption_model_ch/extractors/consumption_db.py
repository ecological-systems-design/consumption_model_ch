import numpy as np
import pandas as pd
from pathlib import Path
from copy import copy, deepcopy
import re, string, warnings
import country_converter as coco

import bw2data as bd

from ..data import get_consumption_df


# Number of relevant columns in the raw file (df_raw) to extract info about activity
N_ACT_RELEVANT = 11
# Index of the column where activities start
FIRST_ACT_IND = 7
# Number of columns that contain info about one activity
N_COLUMNS_INPUT_ACTIVITY = 5

# Column names for exchanges needed by brightway
EXC_COLUMNS_DICT = {
    'name': 'A',
    'reference product': 'B',
    'location': 'C',
    'amount': 'D',
    'unit': 'E',
    'database': 'F',
    'type': 'G',
    'categories': 'H',
    'original_on': 'I',
    'original_activity': 'J',
    'original_db_act': 'K',
    'original_cfl_act': 'L',
    'original_amount': 'M',
    'comment': 'N',
}

# Conversion from type in databases to type that should be in excel file to import a new database
ACTIVITY_TYPE_DICT = {
    'process': 'technosphere',
    'emission': 'biosphere',
}

# Units conversion for consistency TODO this probably already exists somewhere in brightway
UNIT_DICT = {
    'kg': 'kilogram',
    'lt': 'litre'
}

DB_COLUMN = 'F'


def get_habe_filepath(directory, year, tag):
    files = [x for x in directory if x.is_file() and (year in x) and (tag in x)]
    assert len(files) == 1
    return directory / files[0]


def append_exchanges(df, df_ind, df_act, exclude_dbs=(), replace_agribalyse_with_ei=True):
    """Add all exchanges (input activities) from the row df_ind to consumption database dataframe."""

    # Add exchanges column names
    df = append_exchanges_column_names(df)

    # Add first exchange that is the same as the activity itself, type of this exchange is production
    df_act_dict = df_act.set_index('A').to_dict()['B']
    df = append_first_exchange(df, df_act_dict)

    # Add all exchanges
    n_exchanges = (len(df_ind) - FIRST_ACT_IND) // N_COLUMNS_INPUT_ACTIVITY
    #     if n_exchanges != (len(df_ind) - FIRST_ACT_IND) / N_COLUMNS_INPUT_ACTIVITY:
    #         print('smth is not right with exchanges of Activity -> ' + str(df_ind['Translated name']))

    if df_act_dict['unit'] == 'CHF':
        # For activities that have only exiobase exchanges, conversion factor is multiplied by 1e6
        # We assume that activities that have exiobase exchanges, have ONLY exiobase exchanges
        ConversionDem2FU = df_ind['ConversionDem2FU']
    else:
        ConversionDem2FU = df_ind['ConversionDem2FU']

    skip = 0
    for j in range(1, n_exchanges + 1):

        start = FIRST_ACT_IND + N_COLUMNS_INPUT_ACTIVITY * (j - 1) + skip
        end = start + N_COLUMNS_INPUT_ACTIVITY
        df_ind_j = df_ind[start:end]

        # Check that df_ind_j contains <On 1, Activity 1, DB Act 1, CFL Act 1, Amount Act 1> pattern
        flag = 1
        while flag:
            flag_pattern = is_pattern_correct(df_ind_j)
            if flag_pattern == 1:  # we don't need to skip if patter is correct
                flag = 0
            else:
                skip += 1
                start = FIRST_ACT_IND + N_COLUMNS_INPUT_ACTIVITY * (j - 1) + skip
                end = start + N_COLUMNS_INPUT_ACTIVITY
                df_ind_j = df_ind[start:end]

        df = append_one_exchange(
            df,
            df_ind_j,
            ConversionDem2FU,
            exclude_dbs=exclude_dbs,
            replace_agribalyse_with_ei=replace_agribalyse_with_ei
        )

    return df


def create_input_act_dict(act_bw, input_act_amount):
    """Create a dictionary with all info about input activities."""

    input_act_values_dict = {
        'name': act_bw['name'],
        'location': act_bw['location'],
        'amount': input_act_amount,
        'unit': act_bw['unit'],
        'database': act_bw['database'],
        # We do not expect type biosphere, but assign it via ACTIVITY_TYPE_DICT anyway
        # to be sure that we don't encounter them.
    }
    try:
        input_act_values_dict['type'] = ACTIVITY_TYPE_DICT[act_bw['type']]
    except:
        input_act_values_dict['type'] = ACTIVITY_TYPE_DICT[
            'process']  # TODO not great code, but exiobase 2.2 activities don't have a type
    try:
        input_act_values_dict['reference product'] = act_bw['reference product']
    except:
        pass

    return input_act_values_dict


def bw_get_activity_info_manually(input_act_str, db_name, input_act_amount):
    # Extract the activity name
    apostrophes = [(m.start(0), m.end(0)) for m in re.finditer("'", input_act_str)]
    if len(apostrophes) == 1:
        ap_start = 0
        ap_end = apostrophes[0][0]
    else:
        ap_start = apostrophes[0][1]
        ap_end = apostrophes[1][0]
    input_act_name = input_act_str[ap_start:ap_end]
    input_act_unit_loc = input_act_str[input_act_str.find("("): input_act_str.find(")") + 1]
    input_act_unit_loc_split = [re.sub('[^-A-Za-z0-9-€-]', ' ', el).rstrip().lstrip()
                                for el in input_act_unit_loc.split(',')]
    input_act_unit = input_act_unit_loc_split[0]
    input_act_location = input_act_unit_loc_split[1]

    # Add comment when activity cannot be found
    input_act_values_dict = {}
    if 'exiobase' in db_name.lower() and "Manufacture of " in input_act_name:
        input_act_name = input_act_name[15:].capitalize()
    input_act_values_dict['name'] = input_act_name
    input_act_values_dict['unit'] = input_act_unit
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        location_iso2 = coco.convert(names=input_act_location, to='ISO2')
    if location_iso2 == "not found":
        location_iso2 = input_act_location
    input_act_values_dict['location'] = location_iso2
    input_act_values_dict['amount'] = input_act_amount
    input_act_values_dict['database'] = db_name
    input_act_values_dict['type'] = ACTIVITY_TYPE_DICT['process']  # TODO remove hardcoding
    input_act_values_dict['comment'] = 'TODO could not find this activity'

    return input_act_values_dict


def replace_one_db(df, db_old_name, db_new_name):
    """Replace database name with a new one (eg in case a newer version is available)."""
    df_updated = copy(df)

    where = np.where(df_updated[DB_COLUMN] == db_old_name)[0]
    if where.shape[0] != 0:
        df_updated[DB_COLUMN][where] = db_new_name

    return df_updated


def update_all_db(
        df,
        update_ecoinvent=True,
        update_agribalyse=True,
        update_exiobase=True,
        use_ecoinvent_371=True,
):
    """Update all databases in the consumption database. TODO make more generic"""

    if update_ecoinvent:
        if use_ecoinvent_371:
            ei_name = 'ecoinvent 3.7.1 cutoff'
        else:
            ei_name = 'ecoinvent 3.6 cutoff'
        df = replace_one_db(df, 'ecoinvent 3.3 cutoff', ei_name)
    else:
        ei_name = 'ecoinvent 3.3 cutoff'
    if update_agribalyse:
        df = replace_one_db(df, 'Agribalyse 1.2', 'Agribalyse 1.3 - {}'.format(ei_name))
    if update_exiobase:
        df = replace_one_db(df, 'EXIOBASE 2.2', "Exiobase 3.8.1 Monetary 2015")

    return df


def compute_act_unit(df, code_unit):
    """
    Depending on whether `Quantity code` is present for a specific activity,
    set unit to the unit of the first input activity or CHF.
    Comments on units from Andi for all codes that start with `mx`:
        - kWh per year for electricity
        - MJ per year for heating
        - cubic meters per year for water supply and wastewater collection
        - number of waste bags per year for refuse collection
        --> we gonna hardcode them ;)
        --> # TODO important Andi's model (total demands excel file) gives per year, but we divide by 12 later on
    """

    unit = df['DB Act 1'].split('(')[1].split(',')[0]

    if unit == 'million \u20ac':
        unit = 'CHF'

    if 'Quantity code' in df.keys():
        name = df['Translated name'].lower()
        code = df['Quantity code']
        if 'electricity' in name:
            unit = 'kilowatt hour'
        elif 'heating' in name:
            unit = 'megajoule'
        elif 'water supply' in name:
            unit = 'cubic meter'
        elif 'wastewater collection' in name:
            unit = 'cubic meter'
        elif 'refuse collection' in name:
            unit = "number of waste bags"
        elif code in code_unit.keys():
            unit = UNIT_DICT[code_unit[code]]

    return unit


def append_activity(df, df_ind, code_unit):
    """Append activity from row df_ind to the dataframe df in the brightway format."""

    # Append empty row
    df = df.append(pd.Series(), ignore_index=True)
    # Extract activity information
    act_name = df_ind['Translated name']
    if 'Quantity code' in df_ind.index:
        act_code = df_ind['Quantity code']
    else:
        act_code = df_ind['Variable code']
    act_unit = compute_act_unit(df_ind, code_unit)

    len_df = len(df)
    act_data = [['Activity', act_name],
                ['reference product', act_name],
                ['code', act_code],
                ['location', 'CH'],
                ['amount', 1],
                ['unit', act_unit],
                ['original_ConversionDem2FU', df_ind['ConversionDem2FU']]]

    df_act = pd.DataFrame(act_data,
                          columns=list('AB'),
                          index=np.arange(len_df, len_df + len(act_data)))

    df = df.append(df_act, sort=False)

    return df, df_act


def append_exchanges_in_correct_columns(df, dict_with_values):
    """Make sure that exchanges values are appended to df in the correct columns."""

    col_names = list(dict_with_values.keys())  # order of columns is determined by this list
    col_excel_literal = [EXC_COLUMNS_DICT[m] for m in col_names]

    if dict_with_values != EXC_COLUMNS_DICT:
        col_data = [dict_with_values[m] for m in col_names]
    else:
        col_data = col_names

    df = df.append(pd.DataFrame([col_data], columns=col_excel_literal, index=[len(df)]), sort=False)

    return df


def append_exchanges_column_names(df):
    """Add column names for exchanges."""

    df = df.append(pd.DataFrame(['Exchanges'], columns=['A'], index=[len(df)]), sort=False)
    df = append_exchanges_in_correct_columns(df, EXC_COLUMNS_DICT)
    return df


def append_first_exchange(df, df_act_dict, db_name):
    """
    Append first exchange which is activity itself, the amount is always 1,
    the database is always the one that is being currently created, type is `production`.
    """

    first_exc_data_dict = {'name': df_act_dict['Activity'],
                           'reference product': df_act_dict['reference product'],
                           'location': df_act_dict['location'],
                           'amount': 1,
                           'unit': df_act_dict['unit'],
                           'database': db_name,
                           'type': 'production',
                           }

    df = append_exchanges_in_correct_columns(df, first_exc_data_dict)

    return df


def is_pattern_correct(df_ind_j):
    '''
    Check that input activity info has correct pattern.
    In case the pattern is not correct, move on to the next 5 columns and check their pattern.
    This is needed because for some input activities some relevant values are missing, eg only 'On' value is present.
    '''
    list_ = list(df_ind_j.index)
    pattern = ['On', 'Activity', 'DB Act', 'CFL Act', 'Amount Act']
    check = [pattern[n] in list_[n] for n in range(N_COLUMNS_INPUT_ACTIVITY)]
    if np.all(check):
        return 1
    else:
        return 0


def append_one_exchange(df, df_ind_j, conversion_dem_to_fu, exclude_dbs=(), replace_agribalyse_with_ei=True):
    """Extract information about one input activity, eg name, unit, location, etc and append it to the dataframe df."""
    # Extract the activity number
    k = int(''.join(c for c in df_ind_j.index[0] if c.isdigit()))
    # Extract information about activity and save it
    original_str = df_ind_j['DB Act ' + str(k)]
    original_db_code = df_ind_j['Activity ' + str(k)]

    # Find this input activity in brightway databases
    db_name = original_db_code.split("'")[1]
    code = original_db_code.split("'")[3]
    original_db_code_tuple = (db_name, code)

    # exclude unnecessary databases
    if db_name.lower() in exclude_dbs:
        return df

    # Compute amount
    original_on = df_ind_j['On ' + str(k)]
    original_amount = df_ind_j['Amount Act ' + str(k)]
    original_cfl = df_ind_j['CFL Act ' + str(k)]
    computed_amount = original_on \
                      * original_amount \
                      * original_cfl \
                      * conversion_dem_to_fu

    if db_name == 'ecoinvent 3.3 cutoff' and 'ecoinvent 3.3 cutoff' not in bd.databases:
        current_project = deepcopy(bd.projects.current)
        bd.projects.set_current('ecoinvent 3.3 cutoff')  # Temporarily switch to ecoinvent 3.3 project
        act_bw = bd.get_activity(original_db_code_tuple)
        bd.projects.set_current(current_project)
        input_act_values_dict = create_input_act_dict(act_bw, computed_amount)
    else:
        try:
            # Find activity using bw functionality
            act_bw = bd.get_activity(original_db_code_tuple)
            input_act_values_dict = create_input_act_dict(act_bw, computed_amount)
        except:
            # If bd.get_activity does not work for whichever reason, fill info manually
            if replace_agribalyse_with_ei and "agribalyse" in db_name.lower():
                db_name = CONSUMPTION_DB_NAME
            input_act_values_dict = bw_get_activity_info_manually(original_str, db_name, computed_amount)

    # Add exchange to the dataframe with database in brightway format
    input_act_values_dict.update(
        {
            'original_on': original_on,
            'original_activity': original_db_code,
            'original_db_act': original_str,
            'original_cfl_act': original_cfl,
            'original_amount': original_amount,
        }
    )
    df = append_exchanges_in_correct_columns(df, input_act_values_dict)

    return df


class ConsumptionDbExtractor(object):

    @classmethod
    def extract(cls):
        df = get_consumption_df()
        return df

    @classmethod
    def extract_habe_units(cls, directory, year):
        """Extract information about units of some activities from HABE metadata."""

        assert Path(directory).exists()

        # Get path of the HABE data description (Datenbeschreibung)
        path_datenbeschreibung = get_habe_filepath(directory, year, 'Datenbeschreibung')

        # Get meta information about units
        mengen_df = pd.read_excel(path_datenbeschreibung, sheet_name='Mengen', skiprows=14, usecols=[1, 2, 4, 7])
        mengen_df.columns = ['category', 'name', 'code', 'unit']
        mengen_df.dropna(subset=['code'], inplace=True)

        # Combine name and category columns together
        temp1 = mengen_df[mengen_df['category'].notnull()][['category', 'code', 'unit']]
        temp1.columns = ['name', 'code', 'unit']
        temp2 = mengen_df[mengen_df['name'].notnull()][['name', 'code', 'unit']]
        mengen_df = pd.concat([temp1, temp2])

        mengen_df.sort_index(inplace=True)

        # Get units for codes
        code_unit = {}
        for i, el in mengen_df.iterrows():
            code = el['code'].lower()
            code_unit[code] = el['unit']

        return code_unit

    @classmethod
    def create_empty_brightway_df(cls, db_name):
        """Create dataframe for a new database in the Brightway format and add the necessary meta information."""
        df = pd.DataFrame([['cutoff', len(EXC_COLUMNS_DICT) + 3], ['database', db_name]], columns=list('AB'))
        df = df.append(pd.Series(), ignore_index=True)
        return df

    @classmethod
    def complete_columns(cls, df):
        """Add missing On columns to the extracted consumption dataframe."""
        column_names = list(df.columns)
        indices = [i for i, el in enumerate(column_names) if 'Activity' in el]
        column_names_complete = copy(column_names)
        n_el_added = 0
        for ind in indices:
            if 'On' not in column_names[ind - 1]:
                act_name = column_names[ind]
                act_number = act_name[act_name.find(' ') + 1:]
                column_names_complete.insert(ind + n_el_added, 'On ' + act_number)
                n_el_added += 1
        df.columns = column_names_complete[:len(column_names)]
        return df

    @classmethod
    def create_consumption_excel(
            cls,
            df,
            exclude_databases,
            directory,
            year,
            write_dir=None,
            replace_agribalyse_with_ecoinvent=True,
    ):
        """Create `consumption_db.xlsx` that contains consumption database in the bw excel format and write it."""

        if write_dir is None:
            write_dir = Path('write_files') / bd.projects.current.lower().replace(" ", "_")
        write_dir.mkdir(exist_ok=True, parents=True)

        act_indices = df.index[df['ConversionDem2FU'].notna()].tolist()  # indices of all activities
        exclude_databases = [exclude_db.lower() for exclude_db in exclude_databases]
        path_new_db = write_dir / 'consumption_db.xlsx'
        if not path_new_db.exists():
            print("--> Creating consumption_db.xlsx")
            code_unit = cls.extract_habe_units(directory, year)
            for ind in act_indices:
                # For each row
                df_ind = df.iloc[ind]
                df_ind = df_ind[df_ind.notna()]
                # Add activity
                df_bw, df_act = append_activity(df_bw, df_ind[:N_ACT_RELEVANT],
                                                code_unit)  # only pass columns relevant to this function
                # Add exchanges
                df_bw = append_exchanges(
                    df_bw,
                    df_ind,
                    df_act,
                    exclude_dbs=exclude_databases,
                    replace_agribalyse_with_ei=replace_agribalyse_with_ecoinvent
                )
            df_bw.columns = list(string.ascii_uppercase[:len(df_bw.columns)])
            # Update to relevant databases and save excel file
            if "3.7.1" in ei_name:
                use_ecoinvent_371 = True
            else:
                use_ecoinvent_371 = False

            if replace_agribalyse_with_ecoinvent:
                df_agribalyse_ei = pd.read_excel(DATADIR / "agribalyse_replaced_with_ecoinvent.xlsx")
                df_bw = df_bw.append(df_agribalyse_ei, ignore_index=True)

            df_bw = update_all_db(df_bw, use_ecoinvent_371=use_ecoinvent_371)
            df_bw.to_excel(path_new_db, index=False, header=False)

        else:
            print("--> Consumption_db.xlsx already exists, reading it")






# def import_consumption_db(
#     habe_path,
#     exclude_databases=(),
#     consumption_db_name=CONSUMPTION_DB_NAME,
#     habe_year='091011',
#     ei_name="ecoinvent 3.7.1 cutoff",
#     write_dir=None,
#     replace_agribalyse_with_ecoinvent=True,
#     exiobase_path=None,
#     sut_path=None,
# ):
    """Function that imports consumption database developed by Andreas Froemelt to a Brightway project.

    Details on the development of the consumption model can be found here: https://doi.org/10.1021/acs.est.8b01452.
    The model was derived from the Swiss household budget survey that was conducted by BAFU. This import relies on
    creating an additional project with ecoinvent 3.3, because this version was used in the original implementation that
    we need for linking (e.g. reference products)

    Parameters
    ----------
    habe_path : Path or str
        Path to the directory that contains original HABE data, specifically to excel files
        `HABE*_Datenbeschreibung_*UOe.xlsx`
    exclude_databases : list of str
        Contains names of databases that were present in A. Froemelt's implementation, but are excluded from the current
        analysis. We assume that ecoinvent is always included, heia is always excluded, but exiobase and agribalyse may
        be excluded or not. Thus, this list can contain `Agribalyse 1.2`, `Agribalyse 1.3`, `exiobase 2.2`, etc.
    consumption_db_name : str
        Name of the consumption database.
    habe_year : str
        String that specifies, which HABE to use. Should be one of the following: `091011`, `121314` or `151617`.
    ei_name : str
        String that defines ecoinvent database name.
    write_dir : Path or str
        This directory will contain intermediate files generated by this import.
    replace_agribalyse_with_ecoinvent : bool
        True if eggs and fish activities from agribalyse should be replaced with the ones in ecoinvent.
    exiobase_path : Path or str
        Path to exiobase files.
    sut_path : Path or str
        Path to directory with SUT files that are available at https://zenodo.org/record/4588235#.YZUiENnMLRY.
            If exiobase 2.2 is used, then it should contain CH_2007.xls,
            if exiobase 3.8 is used, then it should contain CH_2015.xls,

    """

    # if consumption_db_name in bd.databases:
    #     print(consumption_db_name + " database already present!!! No import is needed")
    # else:
    #     # 1. Create write_dir directory if it has not been specified or created.
    #     # if write_dir is None:
    #     #     write_dir = Path('write_files') / bd.projects.current.lower().replace(" ", "_")
    #     # write_dir.mkdir(exist_ok=True, parents=True)
    #
    #     # 2. Make sure that 'ecoinvent 3.3 cutoff' project has been created
    #     if 'ecoinvent 3.3 cutoff' not in bd.projects:
    #         print(
    #             'BW project `ecoinvent 3.3 cutoff` is needed, please run `create_ecoinvent_33_project(path_ei33).`'
    #         )
    #         return

        # # 3. Create `consumption_db.xlsx` that contains consumption database in the bw excel format.
        # # Extract consumption data from the supporting information available at https://doi.org/10.1021/acs.est.8b01452.
        # consumption_model_path = DATADIR / "es8b01452_si_002.xlsx"
        # # Create dataframe that will be our consumption database after we add activities and exchanges from the raw file
        # df_bw = create_df_bw(CONSUMPTION_DB_NAME)
        # # Read data from the consumption model file
        # sheet_name = 'Overview & LCA-Modeling'
        # df_raw = pd.read_excel(consumption_model_path, sheet_name=sheet_name, header=2)
        # # Extract units from HABE
        # code_unit = get_units_habe(habe_path, habe_year)
        # Add ON columns (to fix some formatting issues in the consumption model file)
        # df = complete_columns(df_raw)
        # Parse Andi's excel file

        # act_indices = df.index[df['ConversionDem2FU'].notna()].tolist()  # indices of all activities
        # exclude_databases = [exclude_db.lower() for exclude_db in exclude_databases]
        # path_new_db = write_dir / 'consumption_db.xlsx'
        # if not path_new_db.exists():
        #     print("--> Creating consumption_db.xlsx")
        #     for ind in act_indices:
        #         # For each row
        #         df_ind = df.iloc[ind]
        #         df_ind = df_ind[df_ind.notna()]
        #         # Add activity
        #         df_bw, df_act = append_activity(df_bw, df_ind[:N_ACT_RELEVANT],
        #                                         code_unit)  # only pass columns relevant to this function
        #         # Add exchanges
        #         df_bw = append_exchanges(
        #             df_bw,
        #             df_ind,
        #             df_act,
        #             exclude_dbs=exclude_databases,
        #             replace_agribalyse_with_ei=replace_agribalyse_with_ecoinvent
        #         )
        #     df_bw.columns = list(string.ascii_uppercase[:len(df_bw.columns)])
        #     # Update to relevant databases and save excel file
        #     if "3.7.1" in ei_name:
        #         use_ecoinvent_371 = True
        #     else:
        #         use_ecoinvent_371 = False
        #
        #     if replace_agribalyse_with_ecoinvent:
        #         df_agribalyse_ei = pd.read_excel(DATADIR / "agribalyse_replaced_with_ecoinvent.xlsx")
        #         df_bw = df_bw.append(df_agribalyse_ei, ignore_index=True)
        #
        #     df_bw = update_all_db(df_bw, use_ecoinvent_371=use_ecoinvent_371)
        #     df_bw.to_excel(path_new_db, index=False, header=False)

